"""Adapter interfaces for every external system the voice assistant touches.

These exist so tool logic never talks to a real API directly. In production
you write a concrete subclass per interface (see the PRODUCTION SEAM notes in
each class) and hand it to :class:`voice_tools.runtime.ToolRuntime`. The
simulated adapters in :mod:`voice_tools.adapters.simulated` implement the
same interfaces with in-memory data, so every behavior below is testable
with zero credentials and zero network.
"""

from abc import ABC, abstractmethod


class AdapterError(Exception):
    """Base class for every failure an adapter may report."""


class SlotTakenError(AdapterError):
    """Raised when a booking slot was taken between lookup and booking.

    Carries real alternatives from the underlying system so the assistant
    can offer them instead of asking the caller to start over.
    """

    def __init__(self, alternatives):
        super().__init__("requested slot is no longer available")
        self.alternatives = list(alternatives)


class NotFoundError(AdapterError):
    """Raised when the requested entity (slot, record) does not exist."""


class BookingAdapter(ABC):
    """Availability + appointment booking for the business's calendar/booking system.

    PRODUCTION SEAM: subclass this and implement the two methods against your
    real booking API (Acuity, Calendly, Mindbody, a custom backend, ...).
    Credentials plug in HERE -- via environment variables or a secrets
    manager -- never in tool logic and never in this repo's history.
    Example::

        class AcuityBookingAdapter(BookingAdapter):
            def __init__(self):
                self.client = acuity_client(api_key=os.environ["ACUITY_API_KEY"])
    """

    @abstractmethod
    def get_availability(self, date: str, service: str) -> list:
        """Return free slot labels (e.g. ``["09:00", "10:30"]``) for a date.

        Must raise :class:`AdapterError` (never return fabricated slots)
        when the backend cannot be reached.
        """

    @abstractmethod
    def book(self, date: str, time: str, name: str, phone: str, service: str) -> dict:
        """Book a slot. Returns e.g. ``{"reference": "BK-123"}``.

        Raises :class:`SlotTakenError` (with alternatives) on conflict and
        :class:`NotFoundError` for unknown slots -- never silently double-books.
        """


class CrmAdapter(ABC):
    """Customer lookup against the business's CRM.

    PRODUCTION SEAM: subclass against HubSpot, GoHighLevel, Salesforce, ...
    using an API key from the environment. Keep a read-through cache here if
    you want the stale-cache fallback path demonstrated in the tests.
    """

    @abstractmethod
    def find_by_phone(self, phone: str) -> dict | None:
        """Return the customer record for a normalized phone number, or None
        for an unknown caller. Raise :class:`AdapterError` on backend failure."""

    def get_cached(self, phone: str) -> dict | None:
        """Best-effort stale record for degraded mode. Default: no cache."""
        return None


class HandoffAdapter(ABC):
    """Escalation path: page/notify a human and record the context.

    PRODUCTION SEAM: subclass to SMS the on-call staffer (Twilio), open a
    ticket, or push to a Slack channel. This is the one adapter that should
    be engineered to essentially never fail -- it is the last resort.
    """

    @abstractmethod
    def page_human(self, reason: str, summary: str, caller: str | None) -> dict:
        """Notify a human. Returns e.g. ``{"reference": "H-123"}``."""
