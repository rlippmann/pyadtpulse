"""ADT Pulse connection. End users should probably not call this directly.

This is the main interface to the http functions to access ADT Pulse.
"""

import logging
import re
from asyncio import AbstractEventLoop
from time import time

from bs4 import BeautifulSoup
from typeguard import typechecked
from yarl import URL

from .const import (
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_SUMMARY_URI,
    ConnectionFailureReason,
)
from .pulse_authentication_properties import PulseAuthenticationProperties
from .pulse_connection_properties import PulseConnectionProperties
from .pulse_connection_status import PulseConnectionStatus
from .pulse_query_manager import PulseQueryManager
from .util import handle_response, make_soup, set_debug_lock

LOG = logging.getLogger(__name__)


SESSION_COOKIES = {"X-mobile-browser": "false", "ICLocal": "en_US"}


class PulseConnection(PulseQueryManager):
    """ADT Pulse connection related attributes."""

    __slots__ = ("_pc_attribute_lock", "_authentication_properties", "_debug_locks")

    @typechecked
    def __init__(
        self,
        pulse_connection_status: PulseConnectionStatus,
        pulse_connection_properties: PulseConnectionProperties,
        pulse_authentication: PulseAuthenticationProperties,
        debug_locks: bool = False,
    ):
        """Initialize ADT Pulse connection."""

        # need to initialize this after the session since we set cookies
        # based on it
        super().__init__(
            pulse_connection_status, pulse_connection_properties, debug_locks
        )
        self._pc_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pc_attribute_lock"
        )
        self._connection_properties = pulse_connection_properties
        self._connection_status = pulse_connection_status
        self._authentication_properties = pulse_authentication
        super().__init__(
            pulse_connection_status,
            pulse_connection_properties,
            debug_locks,
        )
        self._debug_locks = debug_locks

    @typechecked
    async def async_do_login_query(
        self, username: str, password: str, fingerprint: str, timeout: int = 30
    ) -> BeautifulSoup | None:
        """
        Performs a login query to the Pulse site.

        Args:
            timeout (int, optional): The timeout value for the query in seconds.
            Defaults to 30.

        Returns:
            soup: Optional[BeautifulSoup]: A BeautifulSoup object containing
            summary.jsp, or None if failure
            soup: Optional[BeautifulSoup]: A BeautifulSoup object containing
            summary.jsp, or None if failure
        Raises:
            ValueError: if login parameters are not correct
        """

        def extract_seconds_from_string(s: str) -> int:
            seconds = 0
            match = re.search(r"Try again in (\d+)", s)
            if match:
                seconds = int(match.group(1))
                if "minutes" in s:
                    seconds *= 60
            return seconds

        def check_response(
            response: tuple[int, str | None, URL | None]
        ) -> BeautifulSoup | None:
            """Check response for errors."""
            if not handle_response(
                response[0],
                response[2],
                logging.ERROR,
                "Error encountered communicating with Pulse site on login",
            ):
                self._connection_status.connection_failure_reason = (
                    ConnectionFailureReason.UNKNOWN
                )
                return None

            soup = make_soup(
                response[0],
                response[1],
                response[2],
                logging.ERROR,
                "Could not log into ADT Pulse site",
            )
            # FIXME: should probably raise exceptions here
            if soup is None:
                return None
            error = soup.find("div", {"id": "warnMsgContents"})
            if error:
                error_text = error.get_text()
                LOG.error("Error logging into pulse: %s", error_text)
                if retry_after := extract_seconds_from_string(error_text) > 0:
                    self._connection_status.retry_after = int(time()) + retry_after
                return None
            url = self._connection_properties.make_url(ADT_SUMMARY_URI)
            if url != str(response[2]):
                # more specifically:
                # redirect to signin.jsp = username/password error
                # redirect to mfaSignin.jsp = fingerprint error
                # locked out = error == "Sign In unsuccessful. Your account has been
                # locked after multiple sign in attempts.Try again in 30 minutes."
                LOG.error(
                    "Authentication error encountered logging into ADT Pulse"
                    " at location %s",
                    url,
                )
                self._connection_status.connection_failure_reason = (
                    ConnectionFailureReason.INVALID_CREDENTIALS
                )
                return None

            error = soup.find("div", "responsiveContainer")
            if error:
                LOG.error(
                    "2FA authentiation required for ADT pulse username %s: %s",
                    username,
                    error,
                )
                self._connection_status.connection_failure_reason = (
                    ConnectionFailureReason.MFA_REQUIRED
                )
                return None
            return soup

        data = {
            "usernameForm": username,
            "passwordForm": password,
            "networkid": self._authentication_properties.site_id,
            "fingerprint": fingerprint,
        }
        try:
            response = await self.async_query(
                ADT_LOGIN_URI,
                "POST",
                extra_params=data,
                timeout=timeout,
                requires_authentication=False,
            )
            if not handle_response(
                response[0],
                response[2],
                logging.ERROR,
                "Error encountered during ADT login GET",
            ):
                return None
        except Exception as e:  # pylint: disable=broad-except
            LOG.error("Could not log into Pulse site: %s", e)
            self._connection_status.connection_failure_reason = (
                ConnectionFailureReason.UNKNOWN
            )
            return None
        soup = check_response(response)
        if soup is None:
            return None
        self._connection_status.authenticated_flag.set()
        self._authentication_properties.last_login_time = int(time())
        return soup

    @typechecked
    async def async_do_logout_query(self, site_id: str | None) -> None:
        """Performs a logout query to the ADT Pulse site."""
        params = {}
        si = ""
        if site_id is not None and site_id != "":
            self._authentication_properties.site_id = site_id
            si = site_id
        params.update({"networkid": si})

        params.update({"partner": "adt"})
        await self.async_query(
            ADT_LOGOUT_URI,
            extra_params=params,
            timeout=10,
            requires_authentication=False,
        )
        self._connection_status.authenticated_flag.clear()

    @property
    def is_connected(self) -> bool:
        """Check if ADT Pulse is connected."""
        return (
            self._connection_status.authenticated_flag.is_set()
            and self._connection_status.retry_after < int(time())
        )

    def check_sync(self, message: str) -> AbstractEventLoop:
        """Convenience method to check if running from sync context."""
        return self._connection_properties.check_sync(message)

    @property
    def debug_locks(self):
        """Return debug locks."""
        return self._debug_locks
