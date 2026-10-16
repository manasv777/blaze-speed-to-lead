"""Facebook handshake, HMAC, dedupe, and the end-to-end path through the HTTP layer."""
import asyncio
import json

import httpx

from app.fb import build_payload, sign
from app.main import create_app
from app.presets import PRESETS
from app.store import LeadStore

from .conftest import FakeNotifier, FakeQualifier, FakeSheet, good_output, make_pipeline


def payload_for(preset="great", leadgen_id="lead-1"):
    p = PRESETS[preset]
    return build_payload(leadgen_id, "2026-10-16T00:00:00+0000", p["name"], p["email"], p["phone"],
                         p["company"], p["doors"], p["message"])


def run(coro):
    return asyncio.run(coro)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _post(c, payload, secret="test-secret"):
    body = json.dumps(payload).encode()
    return await c.post("/webhook", content=body, headers={"x-hub-signature-256": sign(secret, body)})


def test_verification_handshake(tmp_path):
    pipe = make_pipeline(tmp_path)
    app = create_app(pipe.settings, pipe)

    async def go():
        async with await _client(app) as c:
            ok = await c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
            bad = await c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"})
            return ok, bad

    ok, bad = run(go())
    assert ok.status_code == 200 and ok.text == "12345"
    assert bad.status_code == 403


def test_bad_or_missing_signature_rejected(tmp_path):
    pipe = make_pipeline(tmp_path)
    app = create_app(pipe.settings, pipe)

    async def go():
        async with await _client(app) as c:
            wrong = await _post(c, payload_for(), secret="wrong")
            missing = await c.post("/webhook", content=json.dumps(payload_for()).encode())
            # body tampered after signing
            body = json.dumps(payload_for()).encode()
            sig = sign("test-secret", body)
            tampered = await c.post("/webhook", content=body.replace(b"240", b"999"), headers={"x-hub-signature-256": sig})
            return wrong, missing, tampered

    for r in run(go()):
        assert r.status_code == 401
    assert not (tmp_path / "leads.jsonl").exists()  # nothing processed


def test_end_to_end_all_mocked(tmp_path):
    """LLM, Sheet, and Discord all mocked; real HTTP layer, HMAC, rules, and store."""
    notifier, sheet = FakeNotifier(), FakeSheet()
    pipe = make_pipeline(tmp_path, FakeQualifier(good_output()), notifier, sheet)
    app = create_app(pipe.settings, pipe)

    async def go():
        async with await _client(app) as c:
            r = await _post(c, payload_for("great"))
            await pipe.wait_idle()
            feed = (await c.get("/api/leads")).json()
            return r, feed

    r, feed = run(go())
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert r.json()["ack_ms"] < 1000
    assert len(notifier.sent) == 1 and len(sheet.rows) == 1
    rec, responded_s = notifier.sent[0]
    assert rec["status"] == "scored" and rec["fit_score"] == 5
    assert rec["phone_e164"] == "+15128672291" and rec["doors"] == 240
    assert responded_s < 60
    lead = feed["leads"][0]
    assert lead["status"] == "scored" and lead["latency_ms"] < 60000
    t = lead["timings"]
    assert t["scored_ms"] <= t["alert_sent_ms"] <= t["sheet_logged_ms"]
    assert feed["stats"]["processed"] == 1 and feed["stats"]["scored"] == 1
    # /api/leads exposes no secrets or internals
    blob = json.dumps(feed)
    assert "test-secret" not in blob and "verify-me" not in blob and "phone_raw" not in blob


def test_dedupe_on_leadgen_id_and_email(tmp_path):
    notifier = FakeNotifier()
    pipe = make_pipeline(tmp_path, notifier=notifier)
    app = create_app(pipe.settings, pipe)

    async def go():
        async with await _client(app) as c:
            a = await _post(c, payload_for("great", "lead-1"))
            b = await _post(c, payload_for("great", "lead-1"))  # Facebook retry, same id
            d = await _post(c, payload_for("great", "lead-2"))  # new id, same email
            await pipe.wait_idle()
            return a, b, d, (await c.get("/api/leads")).json()

    a, b, d, feed = run(go())
    assert a.json()["status"] == "accepted"
    assert b.json() == b.json() | {"status": "duplicate", "matched_on": "leadgen_id"}
    assert d.json()["status"] == "duplicate" and d.json()["matched_on"] == "email"
    assert len(notifier.sent) == 1  # alerted exactly once
    assert feed["stats"]["duplicates"] == 2
    assert [l["status"] for l in feed["leads"]].count("duplicate") == 2


def test_dedupe_survives_restart(tmp_path):
    s1 = LeadStore(tmp_path / "leads.jsonl")
    assert s1.check_and_mark("abc", "x@y.com") is None
    s1.append({"leadgen_id": "abc", "email": "x@y.com", "status": "processing"})
    s2 = LeadStore(tmp_path / "leads.jsonl")
    assert s2.check_and_mark("abc", "") == "leadgen_id"
    assert s2.check_and_mark("new", "X@Y.com ") == "email"


def test_demo_submit_goes_through_signed_webhook(tmp_path):
    notifier = FakeNotifier()
    pipe = make_pipeline(tmp_path, notifier=notifier)
    app = create_app(pipe.settings, pipe)

    async def go():
        async with await _client(app) as c:
            page = await c.get("/demo")
            p = PRESETS["great"]
            r = await c.post("/demo/submit", json={**p})
            await pipe.wait_idle()
            return page, r

    page, r = run(go())
    assert page.status_code == 200 and "Lead form" in page.text
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert r.json()["leadgen_id"].startswith("demo-")
    assert len(notifier.sent) == 1
