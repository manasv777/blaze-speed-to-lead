"""Facebook lead-ad webhook helpers: signature check and field_data parsing."""
from __future__ import annotations

import hashlib
import hmac
import uuid

from .models import Lead
from .rules import normalize_phone, parse_doors


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(secret, body), header.strip())


def field_map(field_data: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in field_data or []:
        name = str(item.get("name", "")).strip().lower()
        values = item.get("values") or []
        if name:
            out[name] = ", ".join(str(v) for v in values).strip()
    return out


def parse_lead(payload: dict, received_at: str) -> Lead:
    """Build a Lead from the post-Graph-fetch shape: {leadgen_id, created_time, field_data}."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    fields = field_map(payload.get("field_data", []))
    if not fields:
        raise ValueError("payload has no field_data")
    full_name = fields.get("full_name") or " ".join(
        x for x in (fields.get("first_name", ""), fields.get("last_name", "")) if x
    )
    phone_raw = fields.get("phone_number", "")
    doors_raw = fields.get("number_of_doors", "")
    return Lead(
        # Never drop a lead for a missing id; mint one so it still flows through.
        leadgen_id=str(payload.get("leadgen_id") or f"gen-{uuid.uuid4().hex[:12]}"),
        created_time=str(payload.get("created_time") or ""),
        received_at=received_at,
        full_name=full_name.strip(),
        email=fields.get("email", "").strip(),
        phone_raw=phone_raw,
        phone_e164=normalize_phone(phone_raw),
        company=fields.get("company_name", "").strip(),
        doors_raw=doors_raw,
        doors=parse_doors(doors_raw),
        message=fields.get("message", ""),
    )


def build_payload(
    leadgen_id: str, created_time: str, name: str, email: str, phone: str,
    company: str, doors: str, message: str,
) -> dict:
    """The shape we simulate: what production would have after the Graph API fetch."""
    pairs = [
        ("full_name", name), ("email", email), ("phone_number", phone),
        ("company_name", company), ("number_of_doors", doors), ("message", message),
    ]
    return {
        "leadgen_id": leadgen_id,
        "created_time": created_time,
        "field_data": [{"name": k, "values": [v]} for k, v in pairs],
    }
