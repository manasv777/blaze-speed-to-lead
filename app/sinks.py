"""Outputs: Google Sheet (via Apps Script web app) and the rep alert (Discord / Slack)."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("blaze")

SHEET_COLUMNS = [
    # Lead
    "received_at", "leadgen_id", "name", "email", "phone_e164", "company", "doors", "message",
    # Scoring
    "fit_score", "reason", "rule_overrides",
    # Drafts
    "sms_draft", "email_subject", "email_body",
    # Run info
    "status", "latency_ms", "model",
]


def _sheet_safe(v: object) -> object:
    """Block spreadsheet formula injection from untrusted lead text."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def sheet_row(record: dict) -> dict:
    row = {c: record.get(c) for c in SHEET_COLUMNS}
    if row["fit_score"] is None and record.get("status") == "fallback":
        row["fit_score"] = "UNSCORED"
    row["rule_overrides"] = "; ".join(record.get("rule_overrides") or [])
    return {k: _sheet_safe(v) for k, v in row.items()}


class Sheet(Protocol):
    async def append(self, record: dict) -> None: ...


class AppsScriptSheet:
    def __init__(self, url: str, token: str, http: httpx.AsyncClient):
        self.url, self.token, self.http = url, token, http

    async def append(self, record: dict) -> None:
        # Apps Script answers POST with a 302 to script.googleusercontent.com; follow it.
        r = await self.http.post(
            self.url, json={"token": self.token, "row": sheet_row(record)}, follow_redirects=True
        )
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError(f"Apps Script returned non-JSON (is access set to 'Anyone'?): {r.text[:120]!r}")
        if not body.get("ok"):
            raise RuntimeError(f"Apps Script error: {body.get('error')}")


class MockSheet:
    """Writes rows to data/mock_sheet.csv so you can see exactly what the Sheet would get."""

    def __init__(self, path: Path, delay: float = 0.25):
        self.path, self.delay = path, delay

    async def append(self, record: dict) -> None:
        await asyncio.sleep(self.delay)
        new = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
            if new:
                w.writeheader()
            w.writerow(sheet_row(record))


# ---------- alerts ----------

GREEN, YELLOW, GRAY = 0x2ECC71, 0xF1C40F, 0x95A5A6


def score_color(score: int | None) -> int:
    if score is None:
        return GRAY
    return GREEN if score >= 4 else YELLOW if score == 3 else GRAY


def _clip(s: object, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def score_label(record: dict) -> str:
    if record.get("status") == "fallback":
        return "UNSCORED: call manually"
    return f"{record.get('fit_score')}/5"


def render_text(record: dict, responded_s: float) -> str:
    """Plain-text alert (console notifier, Slack, and the README sample)."""
    ov = record.get("rule_overrides") or []
    lines = [
        f"NEW LEAD  {record.get('name')} | {record.get('company')}",
        f"Score: {score_label(record)}   Status: {record.get('status')}",
        f"Reason: {record.get('reason')}",
        f"Rule overrides: {'; '.join(ov) if ov else 'none'}",
        f"Contact: {record.get('email') or '-'} | {record.get('phone_e164') or '-'} | doors: {record.get('doors') if record.get('doors') is not None else record.get('doors_raw') or '-'}",
        f"Message: {_clip(record.get('message'), 300)}",
        "",
        f"SMS draft ({len(record.get('sms_draft') or '')} chars):",
        record.get("sms_draft") or "",
        "",
        f"Email subject: {record.get('email_subject')}",
        record.get("email_body") or "",
        "",
        f"Responded in {responded_s:.1f}s  (model: {record.get('model')}). Nothing was sent to the lead.",
    ]
    return "\n".join(lines)


def discord_payload(record: dict, responded_s: float) -> dict:
    ov = record.get("rule_overrides") or []
    contact = f"{record.get('email') or '-'}\n{record.get('phone_e164') or record.get('phone_raw') or '-'}"
    doors = record.get("doors") if record.get("doors") is not None else (record.get("doors_raw") or "unknown")
    embed = {
        "title": _clip(f"{score_label(record)}  {record.get('name') or 'Unknown'} | {record.get('company') or '-'}", 256),
        "description": _clip(f"**{record.get('reason')}**\n\n> {_clip(record.get('message'), 500)}", 4000),
        "color": score_color(record.get("fit_score")),
        "fields": [
            {"name": "Contact", "value": _clip(contact, 1024), "inline": True},
            {"name": "Doors", "value": _clip(doors, 64), "inline": True},
            {"name": "Status", "value": str(record.get("status")), "inline": True},
            {"name": "Rule overrides", "value": _clip("; ".join(ov) or "none", 1024), "inline": False},
            {"name": "SMS draft", "value": _clip(record.get("sms_draft"), 1024), "inline": False},
            {"name": f"Email: {_clip(record.get('email_subject'), 200)}", "value": _clip(record.get("email_body"), 1024), "inline": False},
        ],
        "footer": {"text": f"Responded in {responded_s:.1f}s · {record.get('model')} · review before sending"},
    }
    return {
        "content": f"New lead. Responded in {responded_s:.1f}s",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},  # lead text must never ping @everyone
    }


class Notifier(Protocol):
    async def send(self, record: dict, responded_s: float) -> None: ...


class DiscordNotifier:
    def __init__(self, url: str, http: httpx.AsyncClient):
        self.url, self.http = url, http

    async def send(self, record: dict, responded_s: float) -> None:
        payload = discord_payload(record, responded_s)
        r = await self.http.post(self.url, json=payload)
        if r.status_code == 429:  # rate limited: one short wait, then retry
            retry = min(float((r.json() or {}).get("retry_after", 1)), 5.0)
            await asyncio.sleep(retry)
            r = await self.http.post(self.url, json=payload)
        r.raise_for_status()


class SlackNotifier:
    def __init__(self, url: str, http: httpx.AsyncClient):
        self.url, self.http = url, http

    async def send(self, record: dict, responded_s: float) -> None:
        r = await self.http.post(self.url, json={"text": "```" + render_text(record, responded_s) + "```"})
        r.raise_for_status()


class ConsoleNotifier:
    """--mock: prints the alert and appends it to data/mock_alerts.log."""

    def __init__(self, path: Path, delay: float = 0.15):
        self.path, self.delay = path, delay

    async def send(self, record: dict, responded_s: float) -> None:
        await asyncio.sleep(self.delay)
        text = render_text(record, responded_s)
        payload = discord_payload(record, responded_s)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text + "\n" + json.dumps(payload, ensure_ascii=False) + "\n" + "-" * 70 + "\n")
        log.info("ALERT (mock)\n%s", text)
