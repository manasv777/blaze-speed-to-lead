"""Deterministic rules: parsing, normalization, fake detection, score caps.

The model reasons; this module enforces.
"""
from __future__ import annotations

import re

SMALL_PORTFOLIO_DOORS = 10
SMALL_PORTFOLIO_CAP = 2
FAKE_CONTACT_CAP = 1

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}


def parse_doors(raw: object) -> int | None:
    """Parse a free-text door count.

    "240" -> 240, "about 120" -> 120, "50-100" -> 75 (midpoint), "1,200" -> 1200,
    "2k" -> 2000, "a dozen" -> 12, "under 10" -> 9, "asdf" -> None.
    """
    if raw is None:
        return None
    s = str(raw).lower().replace(",", "").strip()
    if not s:
        return None
    s = re.sub(r"(\d+(?:\.\d+)?)\s*k\b", lambda m: str(int(float(m.group(1)) * 1000)), s)
    for word, n in _WORD_NUMS.items():
        s = re.sub(rf"\b{word}\b", str(n), s)
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s)]
    if not nums:
        return None
    if len(nums) >= 2 and re.search(r"\d\s*(?:-|–|—|to)\s*\d", s):
        return int(round((nums[0] + nums[1]) / 2))
    n = nums[0]
    if re.search(r"(?:under|less than|fewer than|below|<)\s*\d", s):
        n = max(n - 1, 0)
    return int(n)


def normalize_phone(raw: object, default_country: str = "1") -> str | None:
    """Normalize to E.164. Assumes US/Canada (+1) when no country code is given."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+$", s)[0]
    has_plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if has_plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


_FAKE_DOMAINS = {
    "test.com", "example.com", "example.org", "example.net", "test.test",
    "asdf.com", "fake.com", "mailinator.com", "nowhere.com",
}
_FAKE_LOCALS = {"test", "asdf", "qwerty", "fake", "none", "noemail", "abc", "aaa", "xxx", "a", "no"}
_FAKE_NAMES = {"test", "asdf", "qwerty", "fake", "test test", "asdf asdf", "xxx", "aaa"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def detect_fake_contact(name: str, email: str, phone_raw: str, phone_e164: str | None) -> list[str]:
    """Return human-readable reasons the contact info looks fake (empty list if fine)."""
    reasons: list[str] = []
    email = (email or "").strip().lower()
    if email:
        if not _EMAIL_RE.match(email):
            reasons.append(f"invalid email '{email}'")
        else:
            local, domain = email.split("@", 1)
            if domain in _FAKE_DOMAINS or local.split("+")[0] in _FAKE_LOCALS:
                reasons.append(f"placeholder email '{email}'")
    if (phone_raw or "").strip():
        if not phone_e164:
            reasons.append(f"unparseable phone '{phone_raw}'")
        elif phone_e164.startswith("+1") and len(phone_e164) == 12:
            d = phone_e164[2:]
            area, exch, line = d[:3], d[3:6], d[6:]
            if (
                len(set(d)) <= 2
                or d in ("1234567890", "0123456789", "9876543210")
                or (exch == "555" and (line.startswith("01") or line == "0000"))
                or area[0] in "01"
                or exch[0] in "01"
            ):
                reasons.append(f"fake-looking phone '{phone_raw}'")
    if (name or "").strip().lower() in _FAKE_NAMES:
        reasons.append(f"placeholder name '{name}'")
    if not email and not (phone_raw or "").strip():
        reasons.append("no email or phone")
    return reasons


def apply_score_rules(
    model_score: int, doors: int | None, fake_reasons: list[str]
) -> tuple[int, list[str]]:
    """Apply caps to the model's score. Returns (final_score, rule_overrides)."""
    score = model_score
    overrides: list[str] = []
    if doors is not None and doors < SMALL_PORTFOLIO_DOORS and score > SMALL_PORTFOLIO_CAP:
        overrides.append(f"doors<{SMALL_PORTFOLIO_DOORS} cap: {score}->{SMALL_PORTFOLIO_CAP}")
        score = SMALL_PORTFOLIO_CAP
    if fake_reasons:
        if score > FAKE_CONTACT_CAP:
            overrides.append(f"fake contact cap: {score}->{FAKE_CONTACT_CAP}")
            score = FAKE_CONTACT_CAP
        overrides.append("FLAG fake contact: " + "; ".join(fake_reasons))
    return score, overrides
