"""Pulse Connection Status."""
from time import time
from asyncio import Event

from typeguard import typechecked
from .const import ConnectionFailureReason
from .util import set_debug_lock


class PulseConnectionStatus:
    """Pulse Connection Status."""

    __slots__ = (
        "_retry_after",
        "_connection_failure_reason",
        "_authenticated_flag",
        "_pcs_attribute_lock",
    )

    @typechecked
    def __init__(self, debug_locks: bool = False):
        self._pcs_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pcs_attribute_lock"
        )
        self._retry_after = int(time())
        self._connection_failure_reason = ConnectionFailureReason.NO_FAILURE
        self._authenticated_flag = Event()

    @property
    def authenticated_flag(self) -> Event:
        """Get the authenticated flag."""
        with self._pcs_attribute_lock:
            return self._authenticated_flag

    @property
    def connection_failure_reason(self) -> ConnectionFailureReason:
        """Get the connection failure reason."""
        with self._pcs_attribute_lock:
            return self._connection_failure_reason

    @connection_failure_reason.setter
    @typechecked
    def connection_failure_reason(self, reason: ConnectionFailureReason) -> None:
        """Set the connection failure reason."""
        with self._pcs_attribute_lock:
            self._connection_failure_reason = reason

    @property
    def retry_after(self) -> int:
        """Get the number of seconds to wait before retrying HTTP requests."""
        with self._pcs_attribute_lock:
            return self._retry_after

    @retry_after.setter
    @typechecked
    def retry_after(self, seconds: int) -> None:
        """Set time after which HTTP requests can be retried."""
        if seconds < time():
            raise ValueError("retry_after cannot be less than current time")
        with self._pcs_attribute_lock:
            self._retry_after = seconds
