"""Sheet + Discord clients against fake HTTP (no network)."""
import asyncio
import json

import httpx
import pytest

from app.sinks import GRAY, GREEN, YELLOW, AppsScriptSheet, DiscordNotifier, discord_payload, sheet_row, SHEET_COLUMNS


def rec(**kw):
    base = {"leadgen_id": "L1", "name": "Dana", "company": "Harborview", "email": "d@h.com", "phone_e164": "+15128672291",
            "doors": 240, "message": "=HYPERLINK(\"http://evil\")", "fit_score": 5, "reason": "good",
            "rule_overrides": ["a", "b"], "sms_draft": "hi", "email_subject": "s", "email_body": "b",
            "status": "scored", "latency_ms": 1234.5, "model": "m"}
    return base | kw


def test_apps_script_follows_redirect_and_sends_token():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url)))
        if req.url.host == "script.google.com":
            body = json.loads(req.content)
            assert body["token"] == "tok" and set(body["row"]) == set(SHEET_COLUMNS)
            return httpx.Response(302, headers={"location": "https://script.googleusercontent.com/echo?x=1"})
        return httpx.Response(200, json={"ok": True, "row": 2})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await AppsScriptSheet("https://script.google.com/macros/s/X/exec", "tok", http).append(rec())

    asyncio.run(go())
    assert seen[0][0] == "POST" and seen[1] == ("GET", "https://script.googleusercontent.com/echo?x=1")


def test_apps_script_error_raises():
    async def go():
        t = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False, "error": "unauthorized"}))
        async with httpx.AsyncClient(transport=t) as http:
            await AppsScriptSheet("https://script.google.com/x", "bad", http).append(rec())

    with pytest.raises(RuntimeError, match="unauthorized"):
        asyncio.run(go())


def test_sheet_row_blocks_formula_injection_and_marks_unscored():
    row = sheet_row(rec())
    assert row["message"].startswith("'=")
    assert row["rule_overrides"] == "a; b"
    assert sheet_row(rec(status="fallback", fit_score=None))["fit_score"] == "UNSCORED"


@pytest.mark.parametrize("score,color", [(5, GREEN), (4, GREEN), (3, YELLOW), (2, GRAY), (1, GRAY), (None, GRAY)])
def test_discord_color_by_score(score, color):
    assert discord_payload(rec(fit_score=score), 1.2)["embeds"][0]["color"] == color


def test_discord_payload_shape_and_limits():
    p = discord_payload(rec(message="x" * 5000, email_body="y" * 3000), 3.14)
    e = p["embeds"][0]
    assert "Responded in 3.1s" in p["content"] and "Responded in 3.1s" in e["footer"]["text"]
    assert p["allowed_mentions"] == {"parse": []}
    assert len(e["description"]) <= 4096 and all(len(f["value"]) <= 1024 for f in e["fields"])
    assert {"SMS draft", "Contact", "Rule overrides"} <= {f["name"] for f in e["fields"]}


def test_discord_retries_once_on_429():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"retry_after": 0.01}) if len(calls) == 1 else httpx.Response(204)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await DiscordNotifier("https://discord.com/api/webhooks/x", http).send(rec(), 1.0)

    asyncio.run(go())
    assert len(calls) == 2
