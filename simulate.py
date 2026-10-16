"""Send a signed, simulated Facebook lead-ad lead to the webhook.

    python simulate.py --preset great
    python simulate.py --random
    python simulate.py --preset junk --url https://abcd.ngrok.app
    python simulate.py --preset great --repeat      # resend the same leadgen_id (Facebook retry)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.config import DEV_APP_SECRET
from app.fb import build_payload, sign
from app.presets import PRESETS, random_lead

ROOT = Path(__file__).resolve().parent


def make_payload(lead: dict, leadgen_id: str | None = None) -> dict:
    return build_payload(
        leadgen_id=leadgen_id or f"sim-{uuid.uuid4().hex[:12]}",
        created_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        name=lead["name"], email=lead["email"], phone=lead["phone"],
        company=lead["company"], doors=lead["doors"], message=lead["message"],
    )


def post_lead(client: httpx.Client, base_url: str, secret: str, payload: dict) -> tuple[httpx.Response, float]:
    body = json.dumps(payload).encode()
    t = time.perf_counter()
    r = client.post(
        base_url.rstrip("/") + "/webhook", content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": sign(secret, body)},
    )
    return r, (time.perf_counter() - t) * 1000


def wait_for_result(client: httpx.Client, base_url: str, leadgen_id: str, timeout: float = 70) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(base_url.rstrip("/") + "/api/leads", params={"ids": leadgen_id})
        for lead in r.json().get("leads", []):
            if lead["status"] in ("scored", "fallback"):
                return lead
        time.sleep(0.5)
    return None


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", choices=sorted(PRESETS))
    g.add_argument("--random", action="store_true")
    ap.add_argument("--url", default=os.getenv("WEBHOOK_BASE_URL", "http://127.0.0.1:8000"),
                    help="server base URL (e.g. your ngrok URL)")
    ap.add_argument("--repeat", action="store_true", help="send the same payload twice to show dedupe")
    ap.add_argument("--no-wait", action="store_true", help="don't poll for the processed result")
    ap.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret (expect 401)")
    args = ap.parse_args()

    secret = os.getenv("FB_APP_SECRET") or DEV_APP_SECRET
    lead = PRESETS[args.preset] if args.preset else random_lead()
    payload = make_payload(lead)
    with httpx.Client(timeout=30) as c:
        r, ms = post_lead(c, args.url, "wrong-secret" if args.bad_signature else secret, payload)
        print(f"POST /webhook -> {r.status_code} in {ms:.0f} ms (client-side): {r.text}")
        if r.status_code != 200:
            sys.exit(1)
        if args.repeat:
            r2, ms2 = post_lead(c, args.url, secret, payload)
            print(f"retry POST   -> {r2.status_code} in {ms2:.0f} ms: {r2.text}")
        if args.no_wait or r.json().get("status") != "accepted":
            return
        res = wait_for_result(c, args.url, payload["leadgen_id"])
        if not res:
            print("No processed result within 70s. Check the server logs.")
            sys.exit(2)
        t = res.get("timings") or {}
        score = res["fit_score"] if res["fit_score"] is not None else "UNSCORED"
        print(f"\n[{args.preset or 'random'}] status={res['status']} score={score} (model said {res.get('model_score')})")
        print(f"  reason:    {res['reason']}")
        print(f"  overrides: {'; '.join(res.get('rule_overrides') or []) or 'none'}")
        print(f"  timeline:  received 0 ms -> scored {t.get('scored_ms')} ms -> alert sent {t.get('alert_sent_ms')} ms "
              f"-> sheet logged {t.get('sheet_logged_ms')} ms")
        print(f"  SMS:       {res['sms_draft']}")
        print(f"  Responded in {(res['latency_ms'] or 0) / 1000:.1f}s | alert_ok={res.get('alert_ok')} sheet_ok={res.get('sheet_ok')} "
              f"| tokens in/out {res.get('input_tokens')}/{res.get('output_tokens')} ${res.get('cost_usd')}")


if __name__ == "__main__":
    random.seed()
    main()
