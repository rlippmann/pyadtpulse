"""ADT Pulse connection. End users should probably not call this directly.

This is the main interface to the http functions to access ADT Pulse.
"""

import logging
import re
from time import time

from aiohttp import ClientSession
from bs4 import BeautifulSoup
from typeguard import typechecked
from yarl import URL

from .const import (
    ADT_DEFAULT_HTTP_ACCEPT_HEADERS,
    ADT_DEFAULT_HTTP_USER_AGENT,
    ADT_DEFAULT_SEC_FETCH_HEADERS,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_SUMMARY_URI,
    ConnectionFailureReason,
)
from .pulse_query_manager import PulseQueryManager
from .util import handle_response, make_soup, set_debug_lock

LOG = logging.getLogger(__name__)


SESSION_COOKIES = {"X-mobile-browser": "false", "ICLocal": "en_US"}


class ADTPulseConnection(PulseQueryManager):
    """ADT Pulse connection related attributes."""

    __slots__ = (
        "_pc_attribute_lock",
        "_last_login_time",
        "_site_id",
    )

    @staticmethod
    @typechecked
    def check_login_parameters(username: str, password: str, fingerprint: str) -> None:
        """Check if login parameters are valid.

        Raises ValueError if a login parameter is not valid.
        """
        if username is None or username == "":
            raise ValueError("Username is mandatory")
        pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        if not re.match(pattern, username):
            raise ValueError("Username must be an email address")
        if password is None or password == "":
            raise ValueError("Password is mandatory")
        if fingerprint is None or fingerprint == "":
            raise ValueError("Fingerprint is required")

    @typechecked
    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        user_agent: str = ADT_DEFAULT_HTTP_USER_AGENT["User-Agent"],
        debug_locks: bool = False,
        detailed_debug_logging: bool = False,
    ):
        """Initialize ADT Pulse connection."""

        # need to initialize this after the session since we set cookies
        # based on it
        self._pc_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pc_attribute_lock"
        )
        super().__init__(host, session, detailed_debug_logging)
        self.service_host = host
        self._session.headers.update({"User-Agent": user_agent})
        self._session.headers.update(ADT_DEFAULT_HTTP_ACCEPT_HEADERS)
        self._session.headers.update(ADT_DEFAULT_SEC_FETCH_HEADERS)
        self._last_login_time = 0

        self._detailed_debug_logging = detailed_debug_logging
        self._site_id = ""

    @property
    def last_login_time(self) -> int:
        """Get the last login time."""
        with self._pc_attribute_lock:
            return self._last_login_time

    @last_login_time.setter
    @typechecked
    def last_login_time(self, login_time: int) -> None:
        with self._pc_attribute_lock:
            self._last_login_time = login_time

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
                self.connection_failure_reason = ConnectionFailureReason.UNKNOWN
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
                    self.retry_after = int(time()) + retry_after
                return None
            if self.make_url(ADT_SUMMARY_URI) != str(response[2]):
                # more specifically:
                # redirect to signin.jsp = username/password error
                # redirect to mfaSignin.jsp = fingerprint error
                # locked out = error == "Sign In unsuccessful. Your account has been
                # locked after multiple sign in attempts.Try again in 30 minutes."
                LOG.error("Authentication error encountered logging into ADT Pulse")
                self.connection_failure_reason = (
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
                self.connection_failure_reason = ConnectionFailureReason.MFA_REQUIRED
                return None
            return soup

        data = {
            "usernameForm": username,
            "passwordForm": password,
            "networkid": self._site_id,
            "fingerprint": fingerprint,
        }
        self.check_login_parameters(username, password, fingerprint)
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
            self.connection_failure_reason = ConnectionFailureReason.UNKNOWN
            return None
        soup = check_response(response)
        if soup is None:
            return None
        self.authenticated_flag.set()
        self.last_login_time = int(time())
        return soup

    @typechecked
    async def async_do_logout_query(self, site_id: str | None) -> None:
        """Performs a logout query to the ADT Pulse site."""
        params = {}
        if site_id is not None:
            self._site_id = site_id
        params.update({"networkid": self._site_id})

        params.update({"partner": "adt"})
        await self.async_query(
            ADT_LOGOUT_URI,
            extra_params=params,
            timeout=10,
            requires_authentication=False,
        )
        self.authenticated_flag.clear()
