"""Fire N signed leads at the webhook and report speed-to-alert.

    python bench.py                 # 10 leads, 3s apart, against localhost
    python bench.py --n 20 --gap 2 --url https://abcd.ngrok.app

Writes out/latency.json and prints p50 / p95 / max for webhook received -> alert sent,
plus the per-step breakdown (LLM, Discord, Sheet) measured server-side.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.config import DEV_APP_SECRET
from app.presets import random_lead
from simulate import make_payload, post_lead

ROOT = Path(__file__).resolve().parent


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, round(p / 100 * (len(v) - 1))))
    return v[k]


def summarize(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {"n": len(values), "p50": round(statistics.median(values), 1), "p95": round(pct(values, 95), 1),
            "max": round(max(values), 1)}


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between leads")
    ap.add_argument("--url", default=os.getenv("WEBHOOK_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=str(ROOT / "out" / "latency.json"))
    args = ap.parse_args()

    secret = os.getenv("FB_APP_SECRET") or DEV_APP_SECRET
    rng = random.Random(args.seed)
    sent: list[dict] = []
    with httpx.Client(timeout=30) as c:
        stats0 = c.get(args.url.rstrip("/") + "/api/leads", params={"limit": 1}).json()["stats"]
        print(f"Benchmarking {args.n} leads, {args.gap}s apart -> {args.url}  (mode: {stats0['mode']})")
        for i in range(args.n):
            payload = make_payload(random_lead(rng), leadgen_id=f"bench-{int(time.time())}-{i}")
            r, ack_ms = post_lead(c, args.url, secret, payload)
            sent.append({"leadgen_id": payload["leadgen_id"], "http": r.status_code, "client_ack_ms": round(ack_ms, 1)})
            print(f"  [{i + 1}/{args.n}] {payload['leadgen_id']} -> {r.status_code} ack {ack_ms:.0f} ms")
            if i < args.n - 1:
                time.sleep(args.gap)

        ids = ",".join(s["leadgen_id"] for s in sent)
        deadline = time.time() + 90
        results: dict[str, dict] = {}
        while time.time() < deadline:
            leads = c.get(args.url.rstrip("/") + "/api/leads", params={"ids": ids, "limit": 500}).json()["leads"]
            results = {l["leadgen_id"]: l for l in leads if l["status"] in ("scored", "fallback")}
            if len(results) == len(sent):
                break
            time.sleep(1)

    rows = []
    for s in sent:
        rec = results.get(s["leadgen_id"], {})
        t = rec.get("timings") or {}
        rows.append(s | {
            "status": rec.get("status", "missing"), "fit_score": rec.get("fit_score"),
            "alert_sent_ms": t.get("alert_sent_ms"), "llm_ms": t.get("llm_ms"),
            "discord_ms": t.get("alert_step_ms"), "sheet_ms": t.get("sheet_step_ms"),
            "server_ack_ms": t.get("ack_ms"), "alert_ok": rec.get("alert_ok"), "sheet_ok": rec.get("sheet_ok"),
            "input_tokens": rec.get("input_tokens"), "output_tokens": rec.get("output_tokens"),
            "cost_usd": rec.get("cost_usd"),
        })

    summary = {
        "received_to_alert_ms": summarize([r["alert_sent_ms"] for r in rows]),
        "llm_ms": summarize([r["llm_ms"] for r in rows]),
        "discord_ms": summarize([r["discord_ms"] for r in rows]),
        "sheet_ms": summarize([r["sheet_ms"] for r in rows]),
        "server_ack_ms": summarize([r["server_ack_ms"] for r in rows]),
        "client_ack_ms": summarize([r["client_ack_ms"] for r in rows]),
    }
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": args.url, "mode": stats0["mode"], "n": args.n, "gap_s": args.gap,
        "statuses": {s: sum(r["status"] == s for r in rows) for s in ("scored", "fallback", "missing")},
        "under_60s": sum(1 for r in rows if r["alert_sent_ms"] is not None and r["alert_sent_ms"] < 60000),
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6),
        "summary": summary, "leads": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")

    fmt = lambda v: "-" if v is None else f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.2f}s"  # noqa: E731
    print(f"\nmode: {out['mode']}   statuses: {out['statuses']}   under 60s: {out['under_60s']}/{args.n}")
    print(f"{'step':<28}{'p50':>9}{'p95':>9}{'max':>9}")
    labels = [("received -> alert sent", "received_to_alert_ms"), ("  LLM (score + drafts)", "llm_ms"),
              ("  Discord post", "discord_ms"), ("  Sheet append (after alert)", "sheet_ms"),
              ("webhook ack (server)", "server_ack_ms"), ("webhook ack (client RTT)", "client_ack_ms")]
    for label, key in labels:
        s = summary[key]
        print(f"{label:<28}{fmt(s['p50']):>9}{fmt(s['p95']):>9}{fmt(s['max']):>9}")
    print(f"total API cost: ${out['total_cost_usd']:.6f}   saved -> {args.out}")


if __name__ == "__main__":
    main()
