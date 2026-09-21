"""Retell AI wiring tests: custom-function definitions + webhook handler.

The same ToolRuntime that drives Vapi function calls must drive Retell custom
functions with zero changes to tool logic. These tests pin that contract.
"""

import json

import pytest

from voice_tools.adapters.simulated import (
    SimulatedBookingAdapter,
    SimulatedCrmAdapter,
    SimulatedHandoffAdapter,
)
from voice_tools.retell import handle_function_call, retell_function_definitions
from voice_tools.runtime import ToolRuntime

DAY = "2026-09-22"


def make_runtime(**overrides):
    calendar = {DAY: {"09:00": None, "10:00": None}}
    defaults = dict(
        booking=SimulatedBookingAdapter(calendar=calendar),
        crm=SimulatedCrmAdapter(contacts={"+15551234567": {"name": "Jordan Lee"}}),
        handoff=SimulatedHandoffAdapter(),
    )
    defaults.update(overrides)
    return ToolRuntime(**defaults)


def test_definitions_mirror_runtime_schemas_with_no_drift():
    """Every tool the runtime exposes must appear as a Retell custom function.

    If someone adds a tool to ToolRuntime.dispatch, this test fails until the
    Retell wiring picks it up -- no silent gaps mid-call.
    """
    runtime = make_runtime()
    defs = retell_function_definitions(runtime)
    schema_names = [s["name"] for s in runtime.function_schemas()]
    assert [d["name"] for d in defs] == schema_names
    assert set(schema_names) == {
        "check_availability",
        "book_appointment",
        "lookup_customer",
        "escalate_to_human",
    }


def test_definitions_carry_name_description_and_parameters():
    runtime = make_runtime()
    for definition in retell_function_definitions(runtime):
        assert definition["name"]
        assert definition["description"]
        params = definition["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


def test_webhook_routes_function_call_to_runtime():
    runtime = make_runtime()
    payload = {
        "function_call": {
            "name": "check_availability",
            "arguments": {"date": DAY, "service": "consult"},
        }
    }
    response = handle_function_call(payload, runtime)
    assert response["result"]["slots"] == ["09:00", "10:00"]


def test_webhook_accepts_json_string_arguments():
    """Retell may deliver arguments as a JSON-encoded string; handle both."""
    runtime = make_runtime()
    payload = {
        "function_call": {
            "name": "lookup_customer",
            "arguments": json.dumps({"phone": "+15551234567"}),
        }
    }
    response = handle_function_call(payload, runtime)
    assert response["result"]["name"] == "Jordan Lee"


def test_webhook_unknown_function_fails_loud():
    """A misconfigured Retell agent must blow up at config time, never silently
    mid-call."""
    runtime = make_runtime()
    payload = {"function_call": {"name": "invent_slots", "arguments": {}}}
    with pytest.raises(KeyError):
        handle_function_call(payload, runtime)


def test_webhook_malformed_payload_fails_loud():
    runtime = make_runtime()
    with pytest.raises(ValueError):
        handle_function_call({"nope": True}, runtime)
    with pytest.raises(ValueError):
        handle_function_call({"function_call": {"arguments": {}}}, runtime)


def test_never_guess_rule_survives_the_retell_path():
    """Backend down mid-call: the Retell path must degrade to fallback/handoff,
    never invent slots, names, or bookings."""
    runtime = make_runtime(
        booking=SimulatedBookingAdapter(
            calendar={DAY: {"09:00": None}}, fail_mode="always"
        )
    )
    payload = {
        "function_call": {
            "name": "book_appointment",
            "arguments": {
                "date": DAY,
                "time": "09:00",
                "name": "Pat",
                "phone": "+15550001111",
                "service": "consult",
            },
        }
    }
    response = handle_function_call(payload, runtime)
    result = response["result"]
    assert result["status"] != "booked"
    # Nothing fabricated: no reference, no invented slot.
    assert "reference" not in result or result.get("reference") is None
    assert result["status"] in {"fallback", "handoff"}
