"""Local JSONL log (the never-lose-a-lead backup) plus the dedupe index."""
from __future__ import annotations

import json
import threading
from pathlib import Path

# Statuses that represent a real (first-seen) lead and so populate the dedupe index.
ORIGINAL_STATUSES = {"processing", "scored", "fallback"}

# Fields /api/leads may expose. Anything else in a record stays server-side.
PUBLIC_FIELDS = [
    "leadgen_id", "received_at", "name", "company", "email", "phone_e164", "doors", "doors_raw",
    "message", "fit_score", "model_score", "reason", "rule_overrides", "status", "duplicate_of",
    "sms_draft", "email_subject", "email_body", "model", "attempts", "llm_error",
    "latency_ms", "timings", "alert_ok", "sheet_ok", "input_tokens", "output_tokens", "cost_usd",
]


class LeadStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._emails: set[str] = set()
        for rec in self._read_raw():
            if rec.get("status") in ORIGINAL_STATUSES:
                self._index(rec.get("leadgen_id", ""), rec.get("email", ""))

    def _index(self, leadgen_id: str, email: str) -> None:
        if leadgen_id:
            self._ids.add(leadgen_id)
        if email:
            self._emails.add(email.strip().lower())

    def check_and_mark(self, leadgen_id: str, email: str) -> str | None:
        """Atomically: return why this is a duplicate, or index it and return None."""
        key = (email or "").strip().lower()
        with self._lock:
            if leadgen_id in self._ids:
                return "leadgen_id"
            if key and key in self._emails:
                return "email"
            self._index(leadgen_id, key)
            return None

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    def _read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn line must not break reads
        return out

    def leads(self) -> list[dict]:
        """One entry per lead (later records for the same key replace earlier ones), newest first."""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for rec in self._read_raw():
            key = rec.get("record_key") or rec.get("leadgen_id", "")
            if key not in merged:
                order.append(key)
            merged[key] = rec
        return [{f: merged[k].get(f) for f in PUBLIC_FIELDS} | {"record_key": k} for k in reversed(order)]
