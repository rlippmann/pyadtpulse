"""Pulse backoff object."""
import datetime
from logging import getLogger
from asyncio import sleep
from time import time

from typeguard import typechecked

from .const import ADT_MAX_BACKOFF
from .util import set_debug_lock

LOG = getLogger(__name__)


class PulseBackoff:
    """Pulse backoff object."""

    __slots__ = (
        "_b_lock",
        "_initial_backoff_interval",
        "_max_backoff_interval",
        "_backoff_count",
        "_expiration_time",
        "_name",
        "_detailed_debug_logging",
        "_threshold",
    )

    @typechecked
    def __init__(
        self,
        name: str,
        initial_backoff_interval: float,
        max_backoff_interval: float = ADT_MAX_BACKOFF,
        threshold: int = 0,
        debug_locks: bool = False,
        detailed_debug_logging=False,
    ) -> None:
        """Initialize backoff."""
        self._b_lock = set_debug_lock(debug_locks, "pyadtpulse._b_lock")
        self._initial_backoff_interval = initial_backoff_interval
        self._max_backoff_interval = max_backoff_interval
        self._backoff_count = 0
        self._expiration_time = 0.0
        self._name = name
        self._detailed_debug_logging = detailed_debug_logging
        self._threshold = threshold

    def _calculate_backoff_interval(self) -> float:
        """Calculate backoff time."""
        if self._backoff_count <= self._threshold:
            return self._initial_backoff_interval
        return min(
            self._initial_backoff_interval
            * (2 ** (self._backoff_count - self._threshold)),
            self._max_backoff_interval,
        )

    def get_current_backoff_interval(self) -> float:
        """Return current backoff time."""
        with self._b_lock:
            return self._calculate_backoff_interval()

    def increment_backoff(self) -> None:
        """Increment backoff."""
        with self._b_lock:
            self._backoff_count += 1
            expiration_time = self._calculate_backoff_interval() + time()

            if expiration_time > self._expiration_time:
                LOG.debug(
                    "Pulse backoff %s: %s expires at %s",
                    self._name,
                    self._backoff_count,
                    datetime.datetime.fromtimestamp(self._expiration_time).strftime(
                        "%m/%d/%Y %H:%M:%S"
                    ),
                )
                self._expiration_time = expiration_time
            else:
                if self._detailed_debug_logging:
                    LOG.debug(
                        "Pulse backoff %s: not updated "
                        "because already expires at %s",
                        self._name,
                        datetime.datetime.fromtimestamp(self._expiration_time).strftime(
                            "%m/%d/%Y %H:%M:%S"
                        ),
                    )

    def reset_backoff(self) -> None:
        """Reset backoff."""
        with self._b_lock:
            if self._expiration_time == 0.0 or time() > self._expiration_time:
                self._backoff_count = 0
                self._backoff_count = 0
                self._expiration_time = 0.0

    @typechecked
    def set_absolute_backoff_time(self, backoff_time: float) -> None:
        """Set absolute backoff time."""
        if backoff_time != 0 and backoff_time < time():
            raise ValueError("backoff_time cannot be less than current time")
        with self._b_lock:
            LOG.debug(
                "Pulse backoff %s: set to %s",
                self._name,
                datetime.datetime.fromtimestamp(backoff_time).strftime(
                    "%m/%d/%Y %H:%M:%S"
                ),
            )
            self._expiration_time = backoff_time

    async def wait_for_backoff(self) -> None:
        """Wait for backoff."""
        with self._b_lock:
            diff = self._expiration_time - time()
            if diff > 0:
                if self._detailed_debug_logging:
                    LOG.debug("Backoff %s: waiting for %s", self._name, diff)
                await sleep(diff)

    def will_backoff(self) -> bool:
        """Return if backoff is needed."""
        with self._b_lock:
            return self._expiration_time >= time()

    @property
    def backoff_count(self) -> int:
        """Return backoff count."""
        with self._b_lock:
            return self._backoff_count

    @property
    def expiration_time(self) -> float:
        """Return backoff expiration time."""
        with self._b_lock:
            return self._expiration_time

    @property
    def initial_backoff_interval(self) -> float:
        """Return initial backoff interval."""
        with self._b_lock:
            return self._initial_backoff_interval

    @initial_backoff_interval.setter
    @typechecked
    def initial_backoff_interval(self, new_interval: float) -> None:
        """Set initial backoff interval."""
        if new_interval <= 0.0:
            raise ValueError("Initial backoff interval must be greater than 0")
        with self._b_lock:
            self._initial_backoff_interval = new_interval

    @property
    def name(self) -> str:
        """Return name."""
        return self._name
