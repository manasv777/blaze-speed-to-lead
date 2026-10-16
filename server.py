"""Run the webhook server.

    python server.py            # real: Claude + Google Sheet + Discord (needs .env)
    python server.py --mock     # mock LLM, Sheet, and Discord (no keys needed)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import time

import uvicorn

from app.config import Settings
from app.main import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="mock the LLM, Sheet, and Discord")
    ap.add_argument("--mock-llm-fail", choices=["timeout", "garbage"], help="make the mock LLM fail to demo the fallback")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="archive data/leads.jsonl first (clears dedupe + demo feed)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.mock_llm_fail:
        os.environ["MOCK_LLM_FAIL"] = args.mock_llm_fail
    settings = Settings.from_env(mock=True if args.mock else None)

    if args.reset:
        for name in ("leads.jsonl", "mock_sheet.csv", "mock_alerts.log"):
            p = settings.data_dir / name
            if p.exists():
                archive = settings.data_dir / "archive"
                archive.mkdir(exist_ok=True)
                shutil.move(p, archive / f"{int(time.time())}-{name}")

    problems = [p for p in settings.problems() if "insecure dev default" not in p]
    if problems:
        raise SystemExit("Missing config for a real run:\n  - " + "\n  - ".join(problems) +
                         "\nFill in .env (see .env.example) or run with --mock.")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
