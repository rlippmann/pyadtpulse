"""Pulse exceptions."""
from time import time

from .pulse_backoff import PulseBackoff


class ExceptionWithBackoff(RuntimeError):
    """Exception with backoff."""

    def __init__(self, message: str, backoff: PulseBackoff):
        """Initialize exception."""
        super().__init__(message)
        self.backoff = backoff
        self.backoff.increment_backoff()


class ExceptionWithRetry(ExceptionWithBackoff):
    """Exception with backoff."""

    def __init__(self, message: str, backoff: PulseBackoff, retry_time: float | None):
        """Initialize exception."""
        super().__init__(message, backoff)
        if retry_time and retry_time > time():
            # don't need a backoff count for absolute backoff
            self.backoff.reset_backoff()
            self.backoff.set_absolute_backoff_time(retry_time)


class PulseServerConnectionError(ExceptionWithBackoff):
    """Server error."""


class PulseClientConnectionError(ExceptionWithBackoff):
    """Client error."""


class PulseServiceTemporarilyUnavailableError(ExceptionWithRetry):
    """Service temporarily unavailable error.

    For HTTP 503 and 429 errors.
    """


class PulseAuthenticationError(ExceptionWithBackoff):
    """Authentication error."""


class PulseAccountLockedError(ExceptionWithRetry):
    """Account locked error."""


class PulseGatewayOfflineError(ExceptionWithBackoff):
    """Gateway offline error."""


class PulseMFARequiredError(ExceptionWithBackoff):
    """MFA required error."""


class PulseNotLoggedInError(Exception):
    """Exception to indicate that the application code is not logged in.

    Used for signalling waiters.
    """
