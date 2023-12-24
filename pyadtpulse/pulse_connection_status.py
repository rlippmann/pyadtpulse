"""Pulse Connection Status."""
from asyncio import Event

from typeguard import typechecked

from .pulse_backoff import PulseBackoff
from .util import set_debug_lock


class PulseConnectionStatus:
    """Pulse Connection Status."""

    __slots__ = (
        "_backoff",
        "_authenticated_flag",
        "_pcs_attribute_lock",
    )

    @typechecked
    def __init__(self, debug_locks: bool = False, detailed_debug_logging=False):
        self._pcs_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pcs_attribute_lock"
        )
        self._backoff = PulseBackoff(
            "Connection Status",
            initial_backoff_interval=1,
            detailed_debug_logging=detailed_debug_logging,
        )
        self._authenticated_flag = Event()

    @property
    def authenticated_flag(self) -> Event:
        """Get the authenticated flag."""
        with self._pcs_attribute_lock:
            return self._authenticated_flag

    @property
    def retry_after(self) -> float:
        """Get the number of seconds to wait before retrying HTTP requests."""
        with self._pcs_attribute_lock:
            return self._backoff.expiration_time

    @retry_after.setter
    @typechecked
    def retry_after(self, seconds: float) -> None:
        """Set time after which HTTP requests can be retried."""
        with self._pcs_attribute_lock:
            self._backoff.set_absolute_backoff_time(seconds)

    def get_backoff(self) -> PulseBackoff:
        """Get the backoff object."""
        return self._backoff

    def increment_backoff(self) -> None:
        """Increment the backoff."""
        with self._pcs_attribute_lock:
            self._backoff.increment_backoff()

    def reset_backoff(self) -> None:
        """Reset the backoff."""
        with self._pcs_attribute_lock:
            self._backoff.reset_backoff()
