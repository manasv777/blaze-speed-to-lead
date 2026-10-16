"""Pydantic models: the parsed lead and the validated LLM output."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationInfo, field_validator, model_validator

SMS_MAX_CHARS = 320
EMAIL_MAX_WORDS = 120
REASON_MAX_CHARS = 160  # strictly under

_PLACEHOLDER_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>|\bX{2,}\b|\bTBD\b|lorem ipsum", re.I)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Pricing / promo language the model may not introduce unless the lead used it first.
_BANNED_TERMS = ["$", "discount", "% off", "percent off", "free trial", "pricing", "price", "promo", "coupon", "guarantee"]


class Lead(BaseModel):
    leadgen_id: str
    created_time: str = ""
    received_at: str
    full_name: str = ""
    email: str = ""
    phone_raw: str = ""
    phone_e164: str | None = None
    company: str = ""
    doors_raw: str = ""
    doors: int | None = None
    message: str = ""

    @property
    def first_name(self) -> str:
        parts = self.full_name.strip().split()
        return parts[0].strip(",.").title() if parts else ""

    def all_text(self) -> str:
        return " ".join(
            [self.full_name, self.email, self.phone_raw, self.phone_e164 or "", self.company,
             self.doors_raw, str(self.doors or ""), self.message]
        )


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def numbers_in(text: str) -> set[str]:
    return {_norm_num(n) for n in _NUM_RE.findall(text or "")}


def allowed_numbers(lead: Lead) -> set[str]:
    allowed = numbers_in(lead.all_text()) | {"15"}  # "15-minute call" is the one sanctioned number
    if lead.phone_e164 and lead.phone_e164.startswith("+1") and len(lead.phone_e164) == 12:
        d = lead.phone_e164[2:]
        allowed |= {d, d[:3], d[3:6], d[6:], "1"}
    return allowed


def validation_context(lead: Lead, booking_url: str) -> dict:
    return {
        "first_name": lead.first_name,
        "allowed_numbers": allowed_numbers(lead),
        "booking_url": booking_url,
        "lead_text": lead.all_text().lower(),
    }


class Qualification(BaseModel):
    """What the model must return. Every rule here is enforced in code."""

    model_config = ConfigDict(extra="forbid")

    fit_score: StrictInt = Field(ge=1, le=5)
    reason: str
    sms_draft: str
    email_subject: str
    email_body: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason is empty")
        if "\n" in v or "\r" in v:
            raise ValueError("reason must be a single line")
        if len(v) >= REASON_MAX_CHARS:
            raise ValueError(f"reason is {len(v)} chars; must be under {REASON_MAX_CHARS}")
        return v

    @field_validator("sms_draft")
    @classmethod
    def _sms(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sms_draft is empty")
        if len(v) > SMS_MAX_CHARS:
            raise ValueError(f"sms_draft is {len(v)} chars; max {SMS_MAX_CHARS}")
        return v

    @field_validator("email_subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        v = v.strip()
        if not v or "\n" in v or len(v) > 120:
            raise ValueError("email_subject must be one non-empty line under 120 chars")
        return v

    @field_validator("email_body")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        words = len(v.split())
        if words == 0:
            raise ValueError("email_body is empty")
        if words > EMAIL_MAX_WORDS:
            raise ValueError(f"email_body is {words} words; max {EMAIL_MAX_WORDS}")
        return v

    @model_validator(mode="after")
    def _content_rules(self, info: ValidationInfo) -> "Qualification":
        ctx = info.context or {}
        errors: list[str] = []
        booking_url = ctx.get("booking_url", "")
        drafts = {"sms_draft": self.sms_draft, "email_subject": self.email_subject, "email_body": self.email_body}

        for field, text in drafts.items():
            stripped = text.replace(booking_url, " ") if booking_url else text
            m = _PLACEHOLDER_RE.search(stripped)
            if m:
                errors.append(f"{field} contains a placeholder '{m.group(0)}'; fill it in or remove it")
            if "allowed_numbers" in ctx:
                invented = sorted(numbers_in(stripped) - ctx["allowed_numbers"])
                if invented:
                    errors.append(f"{field} contains numbers not in the lead data: {invented}")
            lead_text = ctx.get("lead_text", "")
            for term in _BANNED_TERMS:
                if term in stripped.lower() and term not in lead_text:
                    errors.append(f"{field} mentions '{term}'; do not invent pricing, discounts, or promises")

        first = (ctx.get("first_name") or "").lower()
        if first:
            for field in ("sms_draft", "email_body"):
                if not re.search(rf"\b{re.escape(first)}\b", drafts[field].lower()):
                    errors.append(f"{field} must address the lead by first name '{ctx['first_name']}'")
        if booking_url:
            for field in ("sms_draft", "email_body"):
                if booking_url not in drafts[field]:
                    errors.append(f"{field} must include the booking link {booking_url}")
        if errors:
            raise ValueError(" | ".join(errors))
        return self
