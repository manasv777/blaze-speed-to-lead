"""Orchestration: accept (fast, synchronous) then process (background).

Order inside process(): LLM -> rules -> alert the rep -> Sheet -> final JSONL record.
The alert goes before the Sheet on purpose: a slow or broken Sheet can never
delay or block the human getting the lead.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import fb
from .config import Settings
from .llm import Qualifier, QualifyResult, cost_usd, qualify
from .models import Lead
from .rules import apply_score_rules, detect_fake_contact
from .sinks import Notifier, Sheet
from .store import LeadStore

log = logging.getLogger("blaze")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fallback_drafts(lead: Lead, booking_url: str) -> dict:
    first = lead.first_name or "there"
    about = f" about {lead.company}" if lead.company else ""
    return {
        "sms_draft": f"Hi {first}, thanks for reaching out to Blaze. I'd love to hear more{about}. "
                     f"Do you have 15 minutes this week? {booking_url}",
        "email_subject": "Thanks for reaching out to Blaze",
        "email_body": f"Hi {first},\n\nThanks for reaching out to Blaze. I'd love to learn more{about} "
                      f"and see whether we can help with bills, invoicing, and cash forecasting.\n\n"
                      f"Grab a 15-minute call here: {booking_url}\n\nThe Blaze team",
    }


def base_record(lead: Lead) -> dict:
    return {
        "record_key": lead.leadgen_id,
        "leadgen_id": lead.leadgen_id,
        "created_time": lead.created_time,
        "received_at": lead.received_at,
        "name": lead.full_name,
        "email": lead.email,
        "phone_raw": lead.phone_raw,
        "phone_e164": lead.phone_e164,
        "company": lead.company,
        "doors": lead.doors,
        "doors_raw": lead.doors_raw,
        "message": lead.message,
    }


class Pipeline:
    def __init__(self, settings: Settings, store: LeadStore, qualifier: Qualifier, notifier: Notifier, sheet: Sheet):
        self.settings = settings
        self.store = store
        self.qualifier = qualifier
        self.notifier = notifier
        self.sheet = sheet
        self._tasks: set[asyncio.Task] = set()
        self._dup_count = 0
        self._acks: dict[str, float] = {}

    def note_ack(self, leadgen_id: str, ack_ms: float) -> None:
        self._acks[leadgen_id] = round(ack_ms, 1)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self, timeout: float = 90) -> None:
        """Test/bench helper: wait for in-flight background work."""
        end = time.perf_counter() + timeout
        while self._tasks and time.perf_counter() < end:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---- fast path (runs inside the HTTP request, before the ack) ----
    def accept(self, payload: dict, t0: float, received_at: str) -> dict:
        lead = fb.parse_lead(payload, received_at)
        dup = self.store.check_and_mark(lead.leadgen_id, lead.email)
        if dup:
            self._dup_count += 1
            rec = base_record(lead) | {
                "record_key": f"{lead.leadgen_id}#dup{self._dup_count}-{int(time.time() * 1000)}",
                "status": "duplicate",
                "duplicate_of": dup,
                "reason": f"duplicate ({dup} already seen); no alert sent",
                "latency_ms": None,
            }
            self.store.append(rec)
            self._spawn(self._log_sheet_only(rec))
            log.info("duplicate lead %s (matched on %s)", lead.leadgen_id, dup)
            return {"status": "duplicate", "leadgen_id": lead.leadgen_id, "matched_on": dup}
        # Persist before acking: if we crash after this line, the lead is on disk.
        self.store.append(base_record(lead) | {"status": "processing"})
        self._spawn(self.process(lead, t0))
        return {"status": "accepted", "leadgen_id": lead.leadgen_id}

    async def _log_sheet_only(self, rec: dict) -> None:
        try:
            await asyncio.wait_for(self.sheet.append(rec), timeout=self.settings.sheet_timeout_s)
        except Exception as e:
            log.warning("sheet append failed for duplicate %s: %s", rec["leadgen_id"], e)

    # ---- slow path (background) ----
    async def process(self, lead: Lead, t0: float) -> dict:
        ms = lambda: round((time.perf_counter() - t0) * 1000, 1)  # noqa: E731
        timings: dict[str, float] = {}
        rec = base_record(lead)
        try:
            res: QualifyResult = await qualify(self.qualifier, lead, self.settings)
        except Exception as e:  # qualify() shouldn't raise, but the lead must flow regardless
            res = QualifyResult(None, error=f"internal: {e}")
        timings["scored_ms"] = ms()
        timings["llm_ms"] = round(res.llm_ms, 1)

        fake = detect_fake_contact(lead.full_name, lead.email, lead.phone_raw, lead.phone_e164)
        if res.qualification:
            q = res.qualification
            final, overrides = apply_score_rules(q.fit_score, lead.doors, fake)
            rec |= {
                "status": "scored", "fit_score": final, "model_score": q.fit_score, "reason": q.reason,
                "rule_overrides": overrides, "sms_draft": q.sms_draft, "email_subject": q.email_subject,
                "email_body": q.email_body,
            }
        else:
            overrides = ["FLAG fake contact: " + "; ".join(fake)] if fake else []
            rec |= {
                "status": "fallback", "fit_score": None, "model_score": None,
                "reason": f"UNSCORED: call manually ({res.error or 'llm unavailable'})"[:159],
                "rule_overrides": overrides, **fallback_drafts(lead, self.settings.booking_url),
            }
        rec |= {
            "model": self.qualifier.model, "attempts": res.attempts, "llm_error": res.error,
            "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
            "cost_usd": cost_usd(self.settings, res.input_tokens, res.output_tokens),
        }

        # Alert the rep first. Speed matters more than anything else downstream.
        responded_s = (time.perf_counter() - t0)
        t_alert = time.perf_counter()
        try:
            await asyncio.wait_for(self.notifier.send(rec, responded_s), timeout=self.settings.notify_timeout_s)
            rec["alert_ok"] = True
        except Exception as e:
            rec["alert_ok"] = False
            rec["alert_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            log.error("ALERT FAILED for %s: %s", lead.leadgen_id, rec["alert_error"])
        timings["alert_step_ms"] = round((time.perf_counter() - t_alert) * 1000, 1)
        timings["alert_sent_ms"] = ms()
        rec["latency_ms"] = timings["alert_sent_ms"] if rec["alert_ok"] else None
        rec["responded_s_in_alert"] = round(responded_s, 2)

        t_sheet = time.perf_counter()
        try:
            await asyncio.wait_for(self.sheet.append(rec), timeout=self.settings.sheet_timeout_s)
            rec["sheet_ok"] = True
        except Exception as e:
            rec["sheet_ok"] = False
            rec["sheet_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            log.error("sheet append failed for %s (lead is safe in leads.jsonl): %s", lead.leadgen_id, rec["sheet_error"])
        timings["sheet_step_ms"] = round((time.perf_counter() - t_sheet) * 1000, 1)
        timings["sheet_logged_ms"] = ms()

        timings["ack_ms"] = self._acks.pop(lead.leadgen_id, None)
        rec["timings"] = timings
        rec["processed_at"] = now_iso()
        self.store.append(rec)
        log.info(
            "lead %s status=%s score=%s alert=%.0fms (llm %.0fms) sheet_ok=%s",
            lead.leadgen_id, rec["status"], rec["fit_score"], timings["alert_sent_ms"], timings["llm_ms"], rec["sheet_ok"],
        )
        return rec
