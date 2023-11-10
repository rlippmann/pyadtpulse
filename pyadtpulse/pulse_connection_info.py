"""Pulse connection info."""
from asyncio import AbstractEventLoop
from aiohttp import ClientSession

from typeguard import typechecked
from .const import API_HOST_CA, DEFAULT_API_HOST
from .util import set_debug_lock


class PulseConnectionInfo:
    """Pulse connection info."""

    __slots__ = (
        "_api_host",
        "_allocated_session",
        "_session",
        "_loop",
        "_pci_attribute_lock",
        "_detailed_debug_logging",
        "_debug_locks",
    )

    @staticmethod
    @typechecked
    def check_service_host(service_host: str) -> None:
        """Check if service host is valid."""
        if service_host is None or service_host == "":
            raise ValueError("Service host is mandatory")
        if service_host not in (DEFAULT_API_HOST, API_HOST_CA):
            raise ValueError(
                f"Service host must be one of {DEFAULT_API_HOST}" f" or {API_HOST_CA}"
            )

    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        detailed_debug_logging=False,
        debug_locks=False,
    ) -> None:
        """Initialize Pulse connection information."""
        self._pci_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pci_attribute_lock"
        )
        self.debug_locks = debug_locks
        self.detailed_debug_logging = detailed_debug_logging
        self._allocated_session = False
        self._loop: AbstractEventLoop | None = None
        if session is None:
            self._allocated_session = True
            self._session = ClientSession()
        else:
            self._session = session
        self.service_host = host

    def __del__(self):
        """Destructor for ADTPulseConnection."""
        if (
            getattr(self, "_allocated_session", False)
            and getattr(self, "_session", None) is not None
            and not self._session.closed
        ):
            self._session.detach()

    @property
    def service_host(self) -> str:
        """Get the service host."""
        with self._pci_attribute_lock:
            return self._api_host

    @service_host.setter
    @typechecked
    def service_host(self, host: str):
        """Set the service host."""
        self.check_service_host(host)
        with self._pci_attribute_lock:
            self._api_host = host

    @property
    def detailed_debug_logging(self) -> bool:
        """Get the detailed debug logging flag."""
        with self._pci_attribute_lock:
            return self._detailed_debug_logging

    @detailed_debug_logging.setter
    @typechecked
    def detailed_debug_logging(self, value: bool):
        """Set the detailed debug logging flag."""
        with self._pci_attribute_lock:
            self._detailed_debug_logging = value

    @property
    def debug_locks(self) -> bool:
        """Get the debug locks flag."""
        with self._pci_attribute_lock:
            return self._debug_locks

    @debug_locks.setter
    @typechecked
    def debug_locks(self, value: bool):
        """Set the debug locks flag."""
        with self._pci_attribute_lock:
            self._debug_locks = value

    @typechecked
    def check_sync(self, message: str) -> AbstractEventLoop:
        """Checks if sync login was performed.

        Returns the loop to use for run_coroutine_threadsafe if so.
        Raises RuntimeError with given message if not."""
        with self._pci_attribute_lock:
            if self._loop is None:
                raise RuntimeError(message)
            return self._loop

    @property
    def loop(self) -> AbstractEventLoop | None:
        """Get the event loop."""
        with self._pci_attribute_lock:
            return self._loop

    @loop.setter
    @typechecked
    def loop(self, loop: AbstractEventLoop | None):
        """Set the event loop."""
        with self._pci_attribute_lock:
            self._loop = loop
