import pytest
from pydantic import ValidationError

from app.models import Qualification, validation_context

from .conftest import BOOKING, good_output, make_lead


def validate(out, lead=None):
    lead = lead or make_lead()
    return Qualification.model_validate(out, context=validation_context(lead, BOOKING))


def errors_of(out, lead=None) -> str:
    with pytest.raises(ValidationError) as e:
        validate(out, lead)
    return str(e.value)


def test_good_output_passes():
    q = validate(good_output())
    assert q.fit_score == 5


@pytest.mark.parametrize("bad", [0, 6, -1, "4", 4.5, True, None])
def test_score_must_be_int_1_to_5(bad):
    assert "fit_score" in errors_of(good_output(fit_score=bad))


@pytest.mark.parametrize("ok", [1, 3, 5])
def test_score_range_ok(ok):
    assert validate(good_output(fit_score=ok)).fit_score == ok


def test_reason_one_line_under_160():
    assert "single line" in errors_of(good_output(reason="line one\nline two"))
    assert "under 160" in errors_of(good_output(reason="x" * 160))
    assert validate(good_output(reason="x" * 159))


def test_sms_length():
    base = f"Hi Dana, 15-minute call? {BOOKING} "
    assert validate(good_output(sms_draft=base + "a" * (320 - len(base))))
    assert "max 320" in errors_of(good_output(sms_draft=base + "a" * (321 - len(base))))


def test_email_word_count():
    filler = " ".join(["word"] * 110)
    ok = f"Hi Dana, {filler} {BOOKING}"  # 113 words
    assert validate(good_output(email_body=ok))
    too_long = f"Hi Dana, {filler} more more more more more more more more more more {BOOKING}"
    assert "max 120" in errors_of(good_output(email_body=too_long))


@pytest.mark.parametrize("ph", ["[Name]", "{company}", "<first name>", "XXX"])
def test_placeholders_rejected(ph):
    assert "placeholder" in errors_of(good_output(sms_draft=f"Hi Dana {ph}, 15-minute call? {BOOKING}"))


def test_booking_url_itself_is_not_a_placeholder_or_number():
    lead = make_lead()
    url = "https://cal.com/blaze/30min?ref={x}"  # numbers + braces inside the URL are fine
    out = good_output(sms_draft=f"Hi Dana, 15-minute call? {url}", email_body=f"Hi Dana, book here {url}")
    assert Qualification.model_validate(out, context=validation_context(lead, url))


def test_invented_numbers_rejected():
    msg = errors_of(good_output(sms_draft=f"Hi Dana, we save teams 40 hours a month. {BOOKING}"))
    assert "numbers not in the lead data" in msg and "40" in msg


def test_lead_numbers_allowed():
    assert validate(good_output(sms_draft=f"Hi Dana, 240 doors is a lot of payouts. 15 minutes? {BOOKING}"))


def test_invented_pricing_rejected():
    assert "discount" in errors_of(good_output(email_body=f"Hi Dana, we can offer a discount. {BOOKING}"))
    assert "'$'" in errors_of(good_output(email_body=f"Hi Dana, plans start at $ cheap. {BOOKING}"))


def test_first_name_required():
    assert "first name" in errors_of(good_output(sms_draft=f"Hi there, 15-minute call? {BOOKING}"))


def test_booking_link_required():
    assert "booking link" in errors_of(good_output(sms_draft="Hi Dana, want to chat?"))


def test_extra_fields_rejected():
    assert "extra" in errors_of(good_output(discount_code="SAVE"))
