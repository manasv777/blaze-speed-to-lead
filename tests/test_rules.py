import pytest

from app.rules import apply_score_rules, detect_fake_contact, normalize_phone, parse_doors


@pytest.mark.parametrize("raw,expected", [
    ("240", 240), ("about 120", 120), ("~40", 40), ("50-100", 75), ("50 to 100", 75),
    ("1,200", 1200), ("2k", 2000), ("120+", 120), ("a dozen", 12), ("four", 4),
    ("under 10", 9), ("around 300 units", 300), ("asdf", None), ("", None), (None, None),
])
def test_parse_doors(raw, expected):
    assert parse_doors(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("(512) 867-2291", "+15128672291"), ("512.867.2291", "+15128672291"),
    ("1-512-867-2291", "+15128672291"), ("+1 (303) 442-1907", "+13034421907"),
    ("+44 20 7946 0958", "+442079460958"), ("0044 20 7946 0958", "+442079460958"),
    ("512-867-2291 ext 4", "+15128672291"), ("555-0000", None), ("asdf", None), ("", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def _fake(name="Dana Whitfield", email="dana@harborviewpm.com", phone="(512) 867-2291"):
    return detect_fake_contact(name, email, phone, normalize_phone(phone))


def test_real_contact_not_flagged():
    assert _fake() == []
    assert _fake(phone="617-555-8823") == []  # 555 exchange outside the fictional 01xx range is real


@pytest.mark.parametrize("kw", [
    {"email": "test@test.com"}, {"email": "asdf@gmail.com"}, {"email": "dana@example.com"},
    {"email": "not-an-email"}, {"phone": "555-0000"}, {"phone": "(512) 555-0123"},
    {"phone": "111-111-1111"}, {"phone": "123-456-7890"}, {"name": "asdf"},
])
def test_fake_contact_flagged(kw):
    assert _fake(**kw)


def test_small_portfolio_caps_at_2():
    assert apply_score_rules(5, 4, []) == (2, ["doors<10 cap: 5->2"])
    assert apply_score_rules(2, 4, []) == (2, [])
    assert apply_score_rules(5, 10, []) == (5, [])
    assert apply_score_rules(5, None, []) == (5, [])  # unknown doors: no cap


def test_fake_contact_caps_at_1_and_flags():
    score, ov = apply_score_rules(4, 240, ["placeholder email 'test@test.com'"])
    assert score == 1
    assert ov[0] == "fake contact cap: 4->1"
    assert ov[1].startswith("FLAG fake contact")
    score, ov = apply_score_rules(1, 240, ["x"])
    assert score == 1 and ov == ["FLAG fake contact: x"]  # flag recorded even with no change


def test_both_caps_record_both():
    score, ov = apply_score_rules(5, 3, ["x"])
    assert score == 1
    assert ov[:2] == ["doors<10 cap: 5->2", "fake contact cap: 2->1"]
