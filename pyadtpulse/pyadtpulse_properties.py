"""PyADTPulse Properties."""
import logging
import asyncio
import time
from warnings import warn

from aiohttp import ClientSession

from .const import (
    ADT_DEFAULT_HTTP_USER_AGENT,
    ADT_DEFAULT_KEEPALIVE_INTERVAL,
    ADT_DEFAULT_RELOGIN_INTERVAL,
    ADT_MAX_KEEPALIVE_INTERVAL,
    ADT_MIN_RELOGIN_INTERVAL,
    DEFAULT_API_HOST,
    ConnectionFailureReason,
)
from .pulse_connection import ADTPulseConnection
from .site import ADTPulseSite
from .util import set_debug_lock

LOG = logging.getLogger(__name__)


class PyADTPulseProperties:
    """PyADTPulse Properties."""

    __slots__ = (
        "_pulse_connection",
        "_authenticated",
        "_updates_exist",
        "_pp_attribute_lock",
        "_site",
        "_username",
        "_password",
        "_fingerprint",
        "_login_exception",
        "_relogin_interval",
        "_keepalive_interval",
        "_update_succeded",
        "_detailed_debug_logging",
    )

    @staticmethod
    def _check_keepalive_interval(keepalive_interval: int) -> None:
        if keepalive_interval > ADT_MAX_KEEPALIVE_INTERVAL or keepalive_interval <= 0:
            raise ValueError(
                f"keepalive interval ({keepalive_interval}) must be "
                f"greater than 0 and less than {ADT_MAX_KEEPALIVE_INTERVAL}"
            )

    @staticmethod
    def _check_relogin_interval(relogin_interval: int) -> None:
        if relogin_interval < ADT_MIN_RELOGIN_INTERVAL:
            raise ValueError(
                f"relogin interval ({relogin_interval}) must be "
                f"greater than {ADT_MIN_RELOGIN_INTERVAL}"
            )

    def __init__(
        self,
        username: str,
        password: str,
        fingerprint: str,
        service_host: str = DEFAULT_API_HOST,
        user_agent=ADT_DEFAULT_HTTP_USER_AGENT["User-Agent"],
        websession: ClientSession | None = None,
        debug_locks: bool = False,
        keepalive_interval: int = ADT_DEFAULT_KEEPALIVE_INTERVAL,
        relogin_interval: int = ADT_DEFAULT_RELOGIN_INTERVAL,
        detailed_debug_logging: bool = False,
    ) -> None:
        """Create a PyADTPulse properties object.
        Args:
        username (str): Username.
        password (str): Password.
        fingerprint (str): 2FA fingerprint.
        service_host (str, optional): host prefix to use
                     i.e. https://portal.adtpulse.com or
                          https://portal-ca.adtpulse.com
        user_agent (str, optional): User Agent.
                     Defaults to ADT_DEFAULT_HTTP_HEADERS["User-Agent"].
        websession (ClientSession, optional): an initialized
                    aiohttp.ClientSession to use, defaults to None
        debug_locks: (bool, optional): use debugging locks
                    Defaults to False
        keepalive_interval (int, optional): number of minutes between
                    keepalive checks, defaults to ADT_DEFAULT_KEEPALIVE_INTERVAL,
                    maxiumum is ADT_MAX_KEEPALIVE_INTERVAL
        relogin_interval (int, optional): number of minutes between relogin checks
                    defaults to ADT_DEFAULT_RELOGIN_INTERVAL,
                    minimum is ADT_MIN_RELOGIN_INTERVAL
        detailed_debug_logging (bool, optional): enable detailed debug logging
        """
        self._init_login_info(username, password, fingerprint)
        self._pulse_connection = ADTPulseConnection(
            service_host,
            session=websession,
            user_agent=user_agent,
            debug_locks=debug_locks,
            detailed_debug_logging=detailed_debug_logging,
        )
        # FIXME use thread event/condition, regular condition?
        # defer initialization to make sure we have an event loop
        self._login_exception: BaseException | None = None

        self._updates_exist = asyncio.locks.Event()

        self._pp_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.async_attribute_lock"
        )

        self._site: ADTPulseSite | None = None
        self.keepalive_interval = keepalive_interval
        self.relogin_interval = relogin_interval
        self._detailed_debug_logging = detailed_debug_logging
        self._update_succeded = True

    def __repr__(self) -> str:
        """Object representation."""
        return f"<{self.__class__.__name__}: {self._username}>"

    def _init_login_info(self, username: str, password: str, fingerprint: str) -> None:
        """Initialize login info.

        Raises:
            ValueError: if login parameters are not valid.
        """
        ADTPulseConnection.check_login_parameters(username, password, fingerprint)
        self._username = username
        self._password = password
        self._fingerprint = fingerprint

    @property
    def service_host(self) -> str:
        """Get the Pulse host.

        Returns: (str): the ADT Pulse endpoint host
        """
        return self._pulse_connection.service_host

    @service_host.setter
    def service_host(self, host: str) -> None:
        """Override the Pulse host (i.e. to use portal-ca.adpulse.com).

        Args:
            host (str): name of Pulse endpoint host
        """
        ADTPulseConnection.check_service_host(host)
        with self._pp_attribute_lock:
            self._pulse_connection.service_host = host

    def set_service_host(self, host: str) -> None:
        """Backward compatibility for service host property setter."""
        self.service_host = host

    @property
    def username(self) -> str:
        """Get username.

        Returns:
            str: the username
        """
        with self._pp_attribute_lock:
            return self._username

    @property
    def version(self) -> str:
        """Get the ADT Pulse site version.

        Returns:
            str: a string containing the version
        """
        return self._pulse_connection.api_version

    @property
    def relogin_interval(self) -> int:
        """Get re-login interval.

        Returns:
            int: number of minutes to re-login to Pulse
                 0 means disabled
        """
        with self._pp_attribute_lock:
            return self._relogin_interval

    @relogin_interval.setter
    def relogin_interval(self, interval: int | None) -> None:
        """Set re-login interval.

        Args:
            interval (int): The number of minutes between logins.
                            If set to None, resets to ADT_DEFAULT_RELOGIN_INTERVAL

        Raises:
            ValueError: if a relogin interval of less than 10 minutes
                        is specified
        """
        if interval is None:
            interval = ADT_DEFAULT_RELOGIN_INTERVAL
        else:
            self._check_relogin_interval(interval)
        with self._pp_attribute_lock:
            self._relogin_interval = interval
            LOG.debug("relogin interval set to %d", self._relogin_interval)

    @property
    def keepalive_interval(self) -> int:
        """Get the keepalive interval in minutes.

        Returns:
            int: the keepalive interval
        """
        with self._pp_attribute_lock:
            return self._keepalive_interval

    @keepalive_interval.setter
    def keepalive_interval(self, interval: int | None) -> None:
        """Set the keepalive interval in minutes.

        If set to None, resets to ADT_DEFAULT_KEEPALIVE_INTERVAL
        """
        if interval is None:
            interval = ADT_DEFAULT_KEEPALIVE_INTERVAL
        else:
            self._check_keepalive_interval(interval)
        with self._pp_attribute_lock:
            self._keepalive_interval = interval
            LOG.debug("keepalive interval set to %d", self._keepalive_interval)

    @property
    def detailed_debug_logging(self) -> bool:
        """Get the detailed debug logging flag."""
        with self._pp_attribute_lock:
            return self._detailed_debug_logging

    @detailed_debug_logging.setter
    def detailed_debug_logging(self, value: bool) -> None:
        """Set detailed debug logging flag."""
        with self._pp_attribute_lock:
            self._detailed_debug_logging = value
        self._pulse_connection.detailed_debug_logging = value

    @property
    def connection_failure_reason(self) -> ConnectionFailureReason:
        """Get the connection failure reason."""
        return self._pulse_connection.connection_failure_reason

    @property
    def sites(self) -> list[ADTPulseSite]:
        """Return all sites for this ADT Pulse account."""
        warn(
            "multiple sites being removed, use pyADTPulse.site instead",
            PendingDeprecationWarning,
            stacklevel=2,
        )
        with self._pp_attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return [self._site]

    @property
    def site(self) -> ADTPulseSite:
        """Return the site associated with the Pulse login."""
        with self._pp_attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return self._site

    @property
    def is_connected(self) -> bool:
        """Check if connected to ADT Pulse.

        Returns:
            bool: True if connected
        """
        return (
            self._pulse_connection.authenticated_flag.is_set()
            and self._pulse_connection.retry_after < time.time()
        )

    def _set_update_status(self, value: bool) -> None:
        """Sets update failed, sets updates_exist to notify wait_for_update."""
        with self._pp_attribute_lock:
            self._update_succeded = value
            if self._updates_exist is not None and not self._updates_exist.is_set():
                self._updates_exist.set()
