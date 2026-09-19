"""Scenario tests for the three tool integrations: booking/availability,
CRM lookup, and human handoff."""

from datetime import date

from voice_tools.adapters.simulated import (
    SimulatedBookingAdapter,
    SimulatedCrmAdapter,
    SimulatedHandoffAdapter,
)
from voice_tools.runtime import ToolRuntime

TODAY = date(2026, 9, 19)
DAY = "2026-09-22"


def make_runtime(**overrides):
    calendar = {
        DAY: {
            "09:00": None,
            "10:00": None,
            "11:00": {"taken_by": "Existing Client"},
        }
    }
    defaults = dict(
        booking=SimulatedBookingAdapter(calendar=calendar),
        crm=SimulatedCrmAdapter(
            contacts={"+15551234567": {"name": "Jordan Lee"}},
        ),
        handoff=SimulatedHandoffAdapter(),
        timeout=0.5,
        max_retries=1,
        backoff=0.0,
        today=TODAY,
    )
    defaults.update(overrides)
    return ToolRuntime(**defaults)


def test_successful_booking_returns_confirmation_reference():
    runtime = make_runtime()

    result = runtime.book_appointment(
        date=DAY, time="09:00", name="Alex", phone="+15550001111"
    )

    assert result.status == "ok"
    assert result.data["reference"].startswith("BK-")
    assert DAY in result.message and "09:00" in result.message


def test_booking_conflict_offers_alternatives_never_double_books():
    """A just-taken slot -> offer real alternatives. The taken slot must not
    appear among them, and nothing is booked silently."""
    runtime = make_runtime()

    result = runtime.book_appointment(
        date=DAY, time="11:00", name="Alex", phone="+15550001111"
    )

    assert result.status == "fallback"
    assert result.fallback_used is True
    alternatives = result.data["alternatives"]
    assert alternatives, "a conflict must always surface alternatives"
    assert "11:00" not in alternatives
    assert "taken" in result.message.lower()


def test_booking_unknown_slot_is_not_invented():
    """Requesting a time that was never a real slot -> graceful fallback, and
    the availability shown comes from the system, not imagination."""
    runtime = make_runtime()

    result = runtime.book_appointment(
        date=DAY, time="15:00", name="Alex", phone="+15550001111"
    )

    assert result.status == "fallback"
    assert "booked" not in result.message.lower()
    assert result.data["reference"] is None


def test_lookup_known_customer_personalizes_greeting():
    runtime = make_runtime()

    result = runtime.lookup_customer(phone="+1 (555) 123-4567")

    assert result.status == "ok"
    assert result.data["name"] == "Jordan Lee"
    assert "Jordan Lee" in result.message


def test_lookup_unknown_customer_graceful_not_handoff():
    """Unknown number is routine (new caller), not an emergency -> graceful
    fallback greeting, no human needed."""
    runtime = make_runtime()

    result = runtime.lookup_customer(phone="+15550009999")

    assert result.status == "fallback"
    assert "name" not in result.data


def test_lookup_invalid_phone_asks_to_repeat():
    runtime = make_runtime()

    result = runtime.lookup_customer(phone="abc")

    assert result.status == "fallback"
    assert result.attempts == 0
    assert "repeat" in result.message.lower() or "number" in result.message.lower()


def test_past_date_rejected_before_adapter_call():
    runtime = make_runtime()

    result = runtime.check_availability(date="2026-09-01")

    assert result.status == "fallback"
    assert result.attempts == 0


def test_function_schemas_describe_vapi_compatible_tools():
    """The repo ships Vapi-style function schemas so the tools can be pasted
    into a voice-assistant config with no rework."""
    runtime = make_runtime()

    schemas = runtime.function_schemas()
    names = {s["name"] for s in schemas}

    assert {
        "check_availability",
        "book_appointment",
        "lookup_customer",
        "escalate_to_human",
    } <= names
    for schema in schemas:
        assert schema["parameters"]["type"] == "object"
