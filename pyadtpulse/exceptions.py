"""Pulse exceptions."""
from time import time

from .pulse_backoff import PulseBackoff


class PulseExceptionWithBackoff(RuntimeError):
    """Exception with backoff."""

    def __init__(self, message: str, backoff: PulseBackoff):
        """Initialize exception."""
        super().__init__(message)
        self.backoff = backoff
        self.backoff.increment_backoff()


class PulseExceptionWithRetry(PulseExceptionWithBackoff):
    """Exception with backoff."""

    def __init__(self, message: str, backoff: PulseBackoff, retry_time: float | None):
        """Initialize exception."""
        super().__init__(message, backoff)
        self.retry_time = retry_time
        if retry_time and retry_time > time():
            # don't need a backoff count for absolute backoff
            self.backoff.reset_backoff()
            self.backoff.set_absolute_backoff_time(retry_time)


class PulseConnectionError(Exception):
    """Base class for connection errors"""


class PulseServerConnectionError(PulseExceptionWithBackoff, PulseConnectionError):
    """Server error."""


class PulseClientConnectionError(PulseExceptionWithBackoff, PulseConnectionError):
    """Client error."""


class PulseServiceTemporarilyUnavailableError(
    PulseExceptionWithRetry, PulseConnectionError
):
    """Service temporarily unavailable error.

    For HTTP 503 and 429 errors.
    """


class PulseLoginException(Exception):
    """Login exceptions.

    Base class for catching all login exceptions."""


class PulseAuthenticationError(PulseExceptionWithBackoff, PulseLoginException):
    """Authentication error."""


class PulseAccountLockedError(PulseExceptionWithRetry, PulseLoginException):
    """Account locked error."""


class PulseGatewayOfflineError(PulseExceptionWithBackoff):
    """Gateway offline error."""


class PulseMFARequiredError(PulseExceptionWithBackoff, PulseLoginException):
    """MFA required error."""


class PulseNotLoggedInError(PulseExceptionWithBackoff, PulseLoginException):
    """Exception to indicate that the application code is not logged in.

    Used for signalling waiters.
    """
