"""Tests for the ToolRuntime: timeouts, retries, fallbacks, and the
never-guess hard rule.

Every test here is written against the public contract:

* every external call runs under a timeout with retries,
* a failed call degrades to a fallback or escalates to a human,
* the assistant NEVER invents data (no guessed names, times, or bookings).
"""

from datetime import date

import pytest

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
            cache={"+15557654321": {"name": "Sam Rivera (cached)"}},
        ),
        handoff=SimulatedHandoffAdapter(),
        timeout=0.5,
        max_retries=1,
        backoff=0.0,
        today=TODAY,
    )
    defaults.update(overrides)
    return ToolRuntime(**defaults)


def test_timeout_triggers_fallback_not_exception():
    """A slow booking system must degrade to a fallback, never hang the call."""
    booking = SimulatedBookingAdapter(
        calendar={DAY: {"09:00": None}}, latency=5.0  # far beyond the timeout
    )
    runtime = make_runtime(booking=booking, timeout=0.2)

    result = runtime.check_availability(date=DAY)

    assert result.status == "fallback"
    assert result.fallback_used is True
    assert result.attempts >= 1
    # The fallback must be honest about what happened.
    assert "calendar" in result.message.lower() or "trouble" in result.message.lower()


def test_retry_recovers_flaky_adapter():
    """One transient failure followed by success -> clean ok, no fallback."""
    booking = SimulatedBookingAdapter(
        calendar={DAY: {"09:00": None}}, fail_mode="flaky_once"
    )
    runtime = make_runtime(booking=booking, max_retries=2)

    result = runtime.check_availability(date=DAY)

    assert result.status == "ok"
    assert result.fallback_used is False
    assert result.attempts == 2
    assert "09:00" in result.data["slots"]


def test_bad_input_degrades_gracefully_without_calling_adapter():
    """Garbage date -> ask the caller to repeat; the adapter is never touched."""

    class ExplodingAdapter(SimulatedBookingAdapter):
        def get_availability(self, *a, **k):
            raise AssertionError("adapter must not be called on invalid input")

    runtime = make_runtime(booking=ExplodingAdapter(calendar={}))

    result = runtime.check_availability(date="not-a-date")

    assert result.status == "fallback"
    assert result.attempts == 0
    assert "repeat" in result.message.lower() or "valid" in result.message.lower()
    assert result.data.get("slots", []) == []


def test_unresolvable_booking_escalates_to_human_with_caller_facts_only():
    """Booking system totally down -> human handoff. The payload carries only
    caller-supplied facts; nothing is invented about the booking itself."""
    booking = SimulatedBookingAdapter(calendar={DAY: {"09:00": None}}, fail_mode="always")
    handoff = SimulatedHandoffAdapter()
    runtime = make_runtime(booking=booking, handoff=handoff, max_retries=1)

    result = runtime.book_appointment(
        date=DAY, time="09:00", name="Alex", phone="+15550001111"
    )

    assert result.status == "handoff"
    assert result.fallback_used is False
    # No fabricated confirmation may leak into the assistant's wording or data.
    assert "reference" not in result.data or result.data.get("reference") is None
    assert "booked" not in result.message.lower()
    # The human gets the real context.
    assert len(handoff.outbox) == 1
    entry = handoff.outbox[0]
    assert entry["caller"] == "+15550001111"
    assert DAY in entry["summary"] and "09:00" in entry["summary"]


def test_lookup_customer_total_failure_never_invents_a_name():
    """CRM unreachable and nothing cached -> generic greeting. No name is
    guessed, implied, or carried in the payload."""
    crm = SimulatedCrmAdapter(contacts={}, cache={}, fail_mode="always")
    runtime = make_runtime(crm=crm, max_retries=1)

    result = runtime.lookup_customer(phone="+15559998888")

    assert result.status == "fallback"
    assert "name" not in result.data
    assert "Jordan" not in result.message and "Sam" not in result.message


def test_lookup_customer_timeout_uses_stale_cache_transparently():
    """Slow CRM with a cached record -> answer from cache, clearly marked stale,
    and phrased as a question rather than an assertion."""
    crm = SimulatedCrmAdapter(
        contacts={"+15557654321": {"name": "Sam Rivera"}},
        cache={"+15557654321": {"name": "Sam Rivera"}},
        latency=5.0,
        fail_mode="always",
    )
    runtime = make_runtime(crm=crm, timeout=0.2, max_retries=0)

    result = runtime.lookup_customer(phone="+15557654321")

    assert result.status == "fallback"
    assert result.data.get("stale") is True
    assert "older" in result.message.lower() or "?" in result.message


def test_escalate_to_human_records_full_context_in_outbox():
    handoff = SimulatedHandoffAdapter()
    runtime = make_runtime(handoff=handoff)

    result = runtime.escalate_to_human(
        reason="caller upset", summary="Billing dispute, wants a manager", caller="+15550002222"
    )

    assert result.status == "ok"
    assert result.data["reference"].startswith("H-")
    assert len(handoff.outbox) == 1
    assert handoff.outbox[0]["reason"] == "caller upset"


def test_dispatch_routes_vapi_style_function_calls():
    """dispatch() maps assistant function-call names to runtime methods so the
    module can sit behind a Vapi/Twilio function endpoint unchanged."""
    runtime = make_runtime()

    result = runtime.dispatch("check_availability", {"date": DAY})

    assert result.status == "ok"
    assert set(result.data["slots"]) == {"09:00", "10:00"}

    with pytest.raises(KeyError):
        runtime.dispatch("nonexistent_tool", {})
