"""Retell AI wiring: custom-function definitions + webhook handler.

The same :class:`voice_tools.runtime.ToolRuntime` that drives Vapi function
calls drives Retell custom functions with zero changes to tool logic.

Setup (Retell dashboard, per agent):

1. Add one *custom function* per entry returned by
   :func:`retell_function_definitions` (name, description, parameters paste
   straight in).
2. Point the custom function's webhook URL at an endpoint that calls
   :func:`handle_function_call` with Retell's request JSON and returns its
   result as the HTTP response body.

The never-guess rule survives the trip: a failed tool call comes back as a
fallback or a human handoff inside ``result`` -- never invented slots, names,
or bookings.
"""

import json

# Retell delivers a custom-function invocation as JSON. ``arguments`` may
# arrive as an object or as a JSON-encoded string; both are accepted.
_FUNCTION_CALL_KEY = "function_call"


def retell_function_definitions(runtime):
    """Custom-function definitions for a Retell agent, derived 1:1 from the
    runtime's tool schemas so the two can never drift apart.

    Returns a list of ``{"name", "description", "parameters"}`` dicts.
    """
    definitions = []
    for schema in runtime.function_schemas():
        definitions.append(
            {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            }
        )
    return definitions


def handle_function_call(payload, runtime):
    """Handle one Retell custom-function webhook call.

    ``payload`` is Retell's request JSON, expected to carry
    ``{"function_call": {"name": <tool>, "arguments": <dict | str>}}``.

    Returns ``{"result": {...}}`` where the inner dict carries the tool's
    ``status`` (``ok`` / ``fallback`` / ``handoff``), the exact wording the
    assistant should speak as ``message``, and the tool's structured ``data``.

    Raises :class:`ValueError` on a malformed payload and :class:`KeyError`
    on an unknown function name -- fail loud at config time, never silently
    mid-call.
    """
    if not isinstance(payload, dict):
        raise ValueError("Retell webhook payload must be a JSON object")
    call = payload.get(_FUNCTION_CALL_KEY)
    if not isinstance(call, dict):
        raise ValueError('payload must contain a "function_call" object')
    name = call.get("name")
    if not name:
        raise ValueError('function_call must include a "name"')
    arguments = call.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("function_call arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("function_call arguments must be an object")

    # KeyError on unknown names: a misconfigured Retell agent surfaces here,
    # at wiring time, instead of dropping the caller's request mid-call.
    result = runtime.dispatch(name, arguments)
    return {
        "result": {
            "status": result.status,
            "message": result.message,
            "fallback_used": result.fallback_used,
            "attempts": result.attempts,
            **result.data,
        }
    }
