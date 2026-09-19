"""ToolRuntime: every voice-assistant tool call goes through here.

The hard rule, enforced in code:

    The assistant NEVER guesses. A failed tool call degrades to a fallback
    or escalates to a human -- it never invents names, times, or bookings.

Mechanics per call:

* validate inputs BEFORE touching any adapter (bad input never becomes a
  backend call, and the adapter is never blamed for it),
* run the adapter call in a worker thread under ``timeout`` seconds,
* retry transient failures up to ``max_retries`` total attempts,
* on exhausting retries: use the tool's degraded path (fallback), or --
  when there is no honest degraded answer, e.g. money/action on the line --
  escalate to a human (handoff).

``ToolResult.message`` is the exact wording the assistant should speak.
``ToolResult.data`` carries structured facts; on fallback/handoff it
contains ONLY caller-supplied or system-returned facts -- never inventions.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime

from voice_tools.adapters.base import NotFoundError, SlotTakenError

# Result statuses. "ok"       = answered from the live system.
#                "fallback"  = degraded but honest answer (stale data flagged,
#                              callback offer, conflict alternatives, ...).
#                "handoff"   = a human is taking over; the assistant says so.
OK, FALLBACK, HANDOFF = "ok", "fallback", "handoff"


@dataclass
class ToolResult:
    status: str
    message: str                       # exact wording for the assistant to speak
    data: dict = field(default_factory=dict)
    fallback_used: bool = False
    attempts: int = 0                  # adapter attempts made (0 = never called)


class ToolRuntime:
    """Runs tool calls with timeout + retry + fallback/handoff.

    ``max_retries`` is the TOTAL number of adapter attempts (1 = try once).
    ``today`` is injectable so date validation is deterministic in tests.
    """

    def __init__(self, *, booking, crm, handoff,
                 timeout=2.0, max_retries=2, backoff=0.0, today=None):
        self.booking = booking
        self.crm = crm
        self.handoff = handoff
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff = backoff
        self._today = today or date.today()

    # ------------------------------------------------------------------
    # machinery
    # ------------------------------------------------------------------
    def _attempt(self, fn, *args, **kwargs):
        """Run ``fn`` with timeout + retries.

        Returns ``(succeeded, value_or_exception, attempts)``. Never raises:
        the caller decides between fallback and handoff.
        """
        last_exc = None
        attempts = 0
        for _ in range(self.max_retries):
            attempts += 1
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(fn, *args, **kwargs)
                try:
                    return True, future.result(timeout=self.timeout), attempts
                except FuturesTimeoutError as exc:
                    last_exc = exc
                except Exception as exc:      # noqa: BLE001 - adapter failures are data
                    last_exc = exc
            finally:
                # Don't block the call on a hung backend; the orphaned worker
                # finishes on its own and the executor is reclaimed at exit.
                executor.shutdown(wait=False, cancel_futures=True)
            if self.backoff:
                time.sleep(self.backoff)
        return False, last_exc, attempts

    # ------------------------------------------------------------------
    # validation (runs before any adapter call)
    # ------------------------------------------------------------------
    def _valid_date(self, value):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= self._today else None

    @staticmethod
    def _valid_time(value):
        try:
            datetime.strptime(value, "%H:%M")
        except (TypeError, ValueError):
            return None
        return value

    @staticmethod
    def _normalize_phone(raw):
        digits = re.sub(r"\D", "", raw or "")
        return ("+" + digits) if len(digits) >= 7 else None

    # ------------------------------------------------------------------
    # tool 1: booking / availability lookup
    # ------------------------------------------------------------------
    def check_availability(self, date, service="general"):
        """Free slots for a date. Timeout -> honest fallback (callback offer),
        never an invented schedule."""
        if not self._valid_date(date):
            return ToolResult(
                status=FALLBACK,
                message=("I didn't catch a valid date -- could you repeat that? "
                         "For example, 'Tuesday the 22nd'."),
                data={"slots": []},
            )

        def _call():
            try:
                return ("ok", self.booking.get_availability(date=date, service=service))
            except NotFoundError as exc:
                return ("domain", exc)   # a real answer ("no schedule"), not a failure

        succeeded, payload, attempts = self._attempt(_call)
        if succeeded:
            kind, value = payload
            if kind == "domain":
                return ToolResult(
                    status=FALLBACK,
                    message=(f"I don't have availability listed for {date}. "
                             "Would another day work, or should I have the team check?"),
                    data={"date": date, "service": service, "slots": []},
                    attempts=attempts,
                )
            slots = value
            if not slots:
                return ToolResult(
                    status=OK,
                    message=(f"I don't see any openings on {date}. "
                             "Would another day work for you?"),
                    data={"date": date, "service": service, "slots": []},
                    attempts=attempts,
                )
            return ToolResult(
                status=OK,
                message=(f"I have these openings on {date}: {', '.join(slots)}. "
                         "Which works best for you?"),
                data={"date": date, "service": service, "slots": slots},
                attempts=attempts,
            )

        # Degraded path: no live calendar, so offer to take details for a
        # human-confirmed booking instead of guessing at openings.
        return ToolResult(
            status=FALLBACK,
            message=("I'm having trouble reaching the live calendar at the moment, "
                     "so I can't see real-time openings. If you give me a day and "
                     "time that suits you, I'll take your details and have the team "
                     "confirm the booking directly."),
            data={"date": date, "service": service, "slots": [],
                  "offer_callback": True},
            fallback_used=True,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # tool 2: book an appointment (action -> highest honesty bar)
    # ------------------------------------------------------------------
    def book_appointment(self, date, time, name, phone, service="general"):
        """Book a slot. Conflicts surface real alternatives; total backend
        failure escalates to a human -- a booking is never confirmed unless
        the system confirmed it."""
        if not self._valid_date(date):
            return ToolResult(
                status=FALLBACK,
                message="I didn't catch a valid date -- could you repeat that?",
                data={"reference": None},
            )
        if not self._valid_time(time):
            return ToolResult(
                status=FALLBACK,
                message="I didn't catch a valid time -- could you repeat that?",
                data={"reference": None},
            )
        if not (name or "").strip():
            return ToolResult(
                status=FALLBACK,
                message="Could I take your name for the booking?",
                data={"reference": None},
            )
        normalized_phone = self._normalize_phone(phone)
        if not normalized_phone:
            return ToolResult(
                status=FALLBACK,
                message="I didn't quite catch that number -- could you repeat it?",
                data={"reference": None},
            )
        name = name.strip()

        def _call():
            try:
                return ("ok", self.booking.book(
                    date=date, time=time, name=name,
                    phone=normalized_phone, service=service))
            except (SlotTakenError, NotFoundError) as exc:
                return ("domain", exc)   # real answers, not failures: no retry

        succeeded, payload, attempts = self._attempt(_call)
        if succeeded:
            kind, value = payload
            if kind == "ok":
                return ToolResult(
                    status=OK,
                    message=(f"You're booked for {service} on {date} at {time}, "
                             f"{name}. Your confirmation reference is "
                             f"{value['reference']}."),
                    data={"reference": value["reference"], "date": date,
                          "time": time, "name": name,
                          "phone": normalized_phone, "service": service},
                    attempts=attempts,
                )
            if isinstance(value, SlotTakenError):
                alternatives = value.alternatives
                alt_text = ", ".join(alternatives) if alternatives else "nothing else that day"
                return ToolResult(
                    status=FALLBACK,
                    message=(f"That time was just taken. I can offer {alt_text} "
                             "instead -- would one of those work?"),
                    data={"reference": None, "alternatives": alternatives,
                          "requested": {"date": date, "time": time}},
                    fallback_used=True,
                    attempts=attempts,
                )
            # NotFoundError: the requested slot never existed.
            return ToolResult(
                status=FALLBACK,
                message=(f"I don't see {time} as an available slot on {date}. "
                         "Would you like to hear what's actually open that day?"),
                data={"reference": None, "alternatives": [],
                      "requested": {"date": date, "time": time}},
                fallback_used=True,
                attempts=attempts,
            )

        # No honest degraded answer exists for a failed booking: a human
        # follows up with the caller's real details. Nothing is "confirmed".
        summary = (f"Booking attempt failed: {service} on {date} at {time} for "
                   f"{name} ({normalized_phone}). Booking system unreachable "
                   f"after {attempts} attempt(s). Please confirm manually.")
        handoff_result = self._page_human(
            reason="booking system unreachable",
            summary=summary,
            caller=normalized_phone,
        )
        return ToolResult(
            status=HANDOFF,
            message=("I'm unable to complete the booking on the system right now. "
                     "Let me bring in a member of the team to get this sorted "
                     "for you -- I have your details here."),
            data={"handoff_reference": handoff_result.data.get("reference"),
                  "attempted": {"date": date, "time": time, "name": name,
                                "phone": normalized_phone, "service": service}},
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # tool 3: CRM customer lookup
    # ------------------------------------------------------------------
    def lookup_customer(self, phone):
        """Identify the caller. Unknown numbers are routine (new caller), not
        emergencies. Stale cache is flagged and phrased as a question."""
        normalized = self._normalize_phone(phone)
        if not normalized:
            return ToolResult(
                status=FALLBACK,
                message="I didn't quite catch that number -- could you repeat it?",
                data={},
            )

        succeeded, record, attempts = self._attempt(
            self.crm.find_by_phone, normalized)
        if succeeded:
            if record:
                name = record.get("name", "there")
                return ToolResult(
                    status=OK,
                    message=f"Thanks for calling back, {name}. How can I help today?",
                    data={"name": record.get("name"), "phone": normalized},
                    attempts=attempts,
                )
            return ToolResult(
                status=FALLBACK,
                message=("Thanks for calling -- I don't have you in the system yet, "
                         "so I'll make sure we get your details down. "
                         "How can I help?"),
                data={"phone": normalized},
                attempts=attempts,
            )

        cached = self.crm.get_cached(normalized)
        if cached and cached.get("name"):
            return ToolResult(
                status=FALLBACK,
                message=("I may be looking at slightly older information here -- "
                         f"is this {cached['name']}?"),
                data={"name": cached["name"], "phone": normalized, "stale": True},
                fallback_used=True,
                attempts=attempts,
            )
        # Total CRM failure with no cache: greet generically. Inventing a
        # name here would be worse than no personalization at all.
        return ToolResult(
            status=FALLBACK,
            message="Thanks for calling. How can I help you today?",
            data={"phone": normalized},
            fallback_used=True,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # tool 4: human handoff / escalation
    # ------------------------------------------------------------------
    def _page_human(self, reason, summary, caller=None):
        """Low-level page; always returns a ToolResult, even if paging fails."""
        succeeded, payload, attempts = self._attempt(
            self.handoff.page_human, reason, summary, caller)
        if succeeded:
            return ToolResult(
                status=OK,
                message=("I'm bringing in a member of the team now. "
                         "They'll have the full context of this call."),
                data={"reference": payload.get("reference")},
                attempts=attempts,
            )
        return ToolResult(
            status=HANDOFF,
            message="I'm getting someone from the team to join us -- one moment.",
            data={"reference": None, "queued": False},
            attempts=attempts,
        )

    def escalate_to_human(self, reason, summary, caller=None):
        """Explicit escalation tool the assistant calls when it detects it
        cannot resolve the request (upset caller, out-of-scope question, ...)."""
        return self._page_human(reason=reason, summary=summary, caller=caller)

    # ------------------------------------------------------------------
    # voice-platform wiring (Vapi / Twilio-style function calling)
    # ------------------------------------------------------------------
    def dispatch(self, name, arguments):
        """Route a voice platform's function call to the matching tool.

        Raises KeyError on unknown function names -- fail loud at config
        time, not silently mid-call.
        """
        handlers = {
            "check_availability": self.check_availability,
            "book_appointment": self.book_appointment,
            "lookup_customer": self.lookup_customer,
            "escalate_to_human": self.escalate_to_human,
        }
        return handlers[name](**(arguments or {}))

    def function_schemas(self):
        """Vapi-compatible function schemas: paste into the assistant config."""
        return [
            {
                "name": "check_availability",
                "description": "Look up free appointment slots for a date.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string",
                                 "description": "Desired date as YYYY-MM-DD."},
                        "service": {"type": "string",
                                    "description": "Service requested."},
                    },
                    "required": ["date"],
                },
            },
            {
                "name": "book_appointment",
                "description": "Book an appointment slot for the caller.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD."},
                        "time": {"type": "string", "description": "HH:MM (24h)."},
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "service": {"type": "string"},
                    },
                    "required": ["date", "time", "name", "phone"],
                },
            },
            {
                "name": "lookup_customer",
                "description": "Look up the caller in the CRM by phone number.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string",
                                  "description": "Caller's phone number."},
                    },
                    "required": ["phone"],
                },
            },
            {
                "name": "escalate_to_human",
                "description": ("Hand the call to a human team member. Use when the "
                                "request can't be resolved or the caller asks."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string",
                                   "description": "Short reason for escalation."},
                        "summary": {"type": "string",
                                    "description": "Context for the human."},
                        "caller": {"type": "string",
                                    "description": "Caller's phone, if known."},
                    },
                    "required": ["reason", "summary"],
                },
            },
        ]
