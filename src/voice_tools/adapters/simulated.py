"""Simulated adapters: in-memory stand-ins for the booking system, CRM,
and human-handoff channel. Zero network, zero credentials.

Each adapter accepts ``latency`` (simulated backend slowness, seconds) and
``fail_mode`` to deterministically reproduce production failure shapes:

* ``fail_mode=None``       -- healthy backend
* ``fail_mode="flaky_once"`` -- first call raises, then recovers (retry path)
* ``fail_mode="always"``   -- every call raises (fallback/handoff path)
"""

import itertools
import time

from voice_tools.adapters.base import (
    AdapterError,
    BookingAdapter,
    CrmAdapter,
    HandoffAdapter,
    NotFoundError,
    SlotTakenError,
)


class _SimulatedBase:
    def __init__(self, latency=0.0, fail_mode=None):
        self.latency = latency
        self.fail_mode = fail_mode
        self._calls = 0

    def _behave(self):
        """Simulate latency, then apply the configured failure mode."""
        if self.latency:
            time.sleep(self.latency)
        self._calls += 1
        if self.fail_mode == "always":
            raise AdapterError("simulated backend failure")
        if self.fail_mode == "flaky_once" and self._calls == 1:
            raise AdapterError("simulated transient failure")


class SimulatedBookingAdapter(_SimulatedBase, BookingAdapter):
    """In-memory booking calendar.

    ``calendar`` maps ``"YYYY-MM-DD"`` -> ``{"HH:MM": None | {"taken_by": ...}}``.
    """

    _references = itertools.count(1)

    def __init__(self, calendar=None, latency=0.0, fail_mode=None):
        _SimulatedBase.__init__(self, latency=latency, fail_mode=fail_mode)
        # Deep-ish copy so tests can't leak state into each other.
        self._calendar = {
            day: dict(slots) for day, slots in (calendar or {}).items()
        }

    def get_availability(self, date, service):
        self._behave()
        day = self._calendar.get(date)
        if day is None:
            raise NotFoundError(f"no schedule for {date}")
        return sorted(t for t, taken in day.items() if taken is None)

    def book(self, date, time, name, phone, service):
        self._behave()
        day = self._calendar.get(date)
        if day is None or time not in day:
            raise NotFoundError(f"unknown slot {date} {time}")
        if day[time] is not None:
            alternatives = sorted(t for t, taken in day.items() if taken is None)
            raise SlotTakenError(alternatives)
        day[time] = {"taken_by": name, "phone": phone, "service": service}
        ref = f"BK-{next(self._references):04d}"
        return {"reference": ref, "date": date, "time": time}


class SimulatedCrmAdapter(_SimulatedBase, CrmAdapter):
    """In-memory CRM. ``contacts`` maps normalized phone -> record dict."""

    def __init__(self, contacts=None, cache=None, latency=0.0, fail_mode=None):
        _SimulatedBase.__init__(self, latency=latency, fail_mode=fail_mode)
        self._contacts = dict(contacts or {})
        self._cache = dict(cache or {})

    def find_by_phone(self, phone):
        self._behave()
        return self._contacts.get(phone)

    def get_cached(self, phone):
        return self._cache.get(phone)


class SimulatedHandoffAdapter(_SimulatedBase, HandoffAdapter):
    """Handoff channel that records pages in ``outbox`` instead of SMS-ing anyone."""

    _references = itertools.count(1)

    def __init__(self, latency=0.0, fail_mode=None):
        _SimulatedBase.__init__(self, latency=latency, fail_mode=fail_mode)
        self.outbox = []

    def page_human(self, reason, summary, caller=None):
        self._behave()
        ref = f"H-{next(self._references):04d}"
        self.outbox.append(
            {"reference": ref, "reason": reason, "summary": summary, "caller": caller}
        )
        return {"reference": ref}
