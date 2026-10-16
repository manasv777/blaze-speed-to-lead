from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import Completion
from app.models import Lead
from app.pipeline import Pipeline
from app.store import LeadStore

BOOKING = "https://cal.com/blaze-demo/15min"


def make_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        fb_app_secret="test-secret", fb_verify_token="verify-me", booking_url=BOOKING,
        data_dir=tmp_path, mock_llm=True, mock_sheet=True, mock_notify=True, **kw,
    )


def make_lead(**kw) -> Lead:
    base = dict(
        leadgen_id="L1", received_at="2026-10-16T00:00:00Z", full_name="Dana Whitfield",
        email="dana@harborviewpm.com", phone_raw="(512) 867-2291", phone_e164="+15128672291",
        company="Harborview PM", doors_raw="240", doors=240,
        message="Owner payouts are eating my week.",
    )
    return Lead(**(base | kw))


def good_output(first="Dana", **kw) -> dict:
    out = {
        "fit_score": 5,
        "reason": "240 doors and a concrete owner-payout pain.",
        "sms_draft": f"Hi {first}, owner payouts eating your week is exactly what Blaze fixes. 15-minute call? {BOOKING}",
        "email_subject": "Taking owner payouts off your plate",
        "email_body": f"Hi {first},\n\nYou mentioned owner payouts across 240 doors. Blaze pays bills, invoices "
                      f"clients, and forecasts cash.\n\nBook 15 minutes: {BOOKING}\n\nThe Blaze team",
    }
    return out | kw


class FakeQualifier:
    """Scripted LLM: returns each queued item in turn (dict, Exception, or 'hang')."""

    model = "fake-llm"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, system, messages, lead):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        item = self.responses.pop(0) if self.responses else good_output()
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        return Completion(item, f"toolu_{len(self.calls)}", 1000, 200, "tool_use")


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent: list[tuple[dict, float]] = []
        self.fail = fail

    async def send(self, record, responded_s):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((dict(record), responded_s))


class FakeSheet:
    def __init__(self, fail=False, hang=False):
        self.rows: list[dict] = []
        self.fail, self.hang = fail, hang

    async def append(self, record):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("sheet 500")
        self.rows.append(dict(record))


def make_pipeline(tmp_path, qualifier=None, notifier=None, sheet=None, **settings_kw) -> Pipeline:
    s = make_settings(tmp_path, **settings_kw)
    return Pipeline(s, LeadStore(s.leads_path), qualifier or FakeQualifier(), notifier or FakeNotifier(), sheet or FakeSheet())


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)
