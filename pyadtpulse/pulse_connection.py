"""ADT Pulse connection. End users should probably not call this directly."""

import logging
import asyncio
import datetime
import re
import time
from http import HTTPStatus
from random import uniform
from threading import RLock

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from bs4 import BeautifulSoup
from yarl import URL

from .const import (
    ADT_DEFAULT_HTTP_ACCEPT_HEADERS,
    ADT_DEFAULT_HTTP_USER_AGENT,
    ADT_DEFAULT_SEC_FETCH_HEADERS,
    ADT_DEFAULT_VERSION,
    ADT_HTTP_BACKGROUND_URIS,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    ADT_OTHER_HTTP_ACCEPT_HEADERS,
    ADT_SUMMARY_URI,
    API_HOST_CA,
    API_PREFIX,
    DEFAULT_API_HOST,
    ConnectionFailureReason,
)
from .util import DebugRLock, handle_response, make_soup

RECOVERABLE_ERRORS = {
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.GATEWAY_TIMEOUT,
}
RETRY_LATER_ERRORS = {HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.TOO_MANY_REQUESTS}
LOG = logging.getLogger(__name__)

MAX_RETRIES = 3
SESSION_COOKIES = {"X-mobile-browser": "false", "ICLocal": "en_US"}


class ADTPulseConnection:
    """ADT Pulse connection related attributes."""

    _api_version = ADT_DEFAULT_VERSION
    _class_threadlock = RLock()

    __slots__ = (
        "_api_host",
        "_allocated_session",
        "_session",
        "_attribute_lock",
        "_loop",
        "_last_login_time",
        "_retry_after",
        "_authenticated_flag",
        "_detailed_debug_logging",
        "_site_id",
        "_connection_failure_reason",
    )

    @staticmethod
    def check_service_host(service_host: str) -> None:
        """Check if service host is valid."""
        if service_host is None or service_host == "":
            raise ValueError("Service host is mandatory")
        if service_host not in (DEFAULT_API_HOST, API_HOST_CA):
            raise ValueError(
                f"Service host must be one of {DEFAULT_API_HOST}" f" or {API_HOST_CA}"
            )

    @staticmethod
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

    @staticmethod
    def get_http_status_description(status_code: int) -> str:
        """Get HTTP status description."""
        status = HTTPStatus(status_code)
        return status.description

    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        user_agent: str = ADT_DEFAULT_HTTP_USER_AGENT["User-Agent"],
        debug_locks: bool = False,
        detailed_debug_logging: bool = False,
    ):
        """Initialize ADT Pulse connection."""
        self._attribute_lock: RLock | DebugRLock
        if not debug_locks:
            self._attribute_lock = RLock()
        else:
            self._attribute_lock = DebugRLock("ADTPulseConnection._attribute_lock")
        self._allocated_session = False
        self._authenticated_flag = asyncio.Event()
        if session is None:
            self._allocated_session = True
            self._session = ClientSession()
        else:
            self._session = session
        # need to initialize this after the session since we set cookies
        # based on it
        self.service_host = host
        self._session.headers.update({"User-Agent": user_agent})
        self._session.headers.update(ADT_DEFAULT_HTTP_ACCEPT_HEADERS)
        self._session.headers.update(ADT_DEFAULT_SEC_FETCH_HEADERS)
        self._last_login_time: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._retry_after = int(time.time())
        self._detailed_debug_logging = detailed_debug_logging
        self._site_id = ""
        self._connection_failure_reason = ConnectionFailureReason.NO_FAILURE

    def __del__(self):
        """Destructor for ADTPulseConnection."""
        if (
            self._allocated_session
            and self._session is not None
            and not self._session.closed
        ):
            self._session.detach()

    @property
    def api_version(self) -> str:
        """Get the API version."""
        with self._class_threadlock:
            return self._api_version

    @property
    def service_host(self) -> str:
        """Get the host prefix for connections."""
        with self._attribute_lock:
            return self._api_host

    @service_host.setter
    def service_host(self, host: str) -> None:
        """Set the host prefix for connections."""
        self.check_service_host(host)
        with self._attribute_lock:
            self._session.headers.update({"Host": host})
            self._api_host = host

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Get the event loop."""
        with self._attribute_lock:
            return self._loop

    @loop.setter
    def loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Set the event loop."""
        with self._attribute_lock:
            self._loop = loop

    @property
    def last_login_time(self) -> int:
        """Get the last login time."""
        with self._attribute_lock:
            return self._last_login_time

    @property
    def retry_after(self) -> int:
        """Get the number of seconds to wait before retrying HTTP requests."""
        with self._attribute_lock:
            return self._retry_after

    @retry_after.setter
    def retry_after(self, seconds: int) -> None:
        """Set time after which HTTP requests can be retried."""
        if seconds < time.time():
            raise ValueError("retry_after cannot be less than current time")
        with self._attribute_lock:
            self._retry_after = seconds

    @property
    def authenticated_flag(self) -> asyncio.Event:
        """Get the authenticated flag."""
        with self._attribute_lock:
            return self._authenticated_flag

    @property
    def detailed_debug_logging(self) -> bool:
        """Get detailed debug logging."""
        with self._attribute_lock:
            return self._detailed_debug_logging

    @detailed_debug_logging.setter
    def detailed_debug_logging(self, value: bool) -> None:
        """Set detailed debug logging."""
        with self._attribute_lock:
            self._detailed_debug_logging = value

    @property
    def connection_failure_reason(self) -> ConnectionFailureReason:
        """Get the connection failure reason."""
        with self._attribute_lock:
            return self._connection_failure_reason

    def _set_connection_failure_reason(self, reason: ConnectionFailureReason) -> None:
        """Set the connection failure reason."""
        with self._attribute_lock:
            self._connection_failure_reason = reason

    def check_sync(self, message: str) -> asyncio.AbstractEventLoop:
        """Checks if sync login was performed.

        Returns the loop to use for run_coroutine_threadsafe if so.
        Raises RuntimeError with given message if not."""
        with self._attribute_lock:
            if self._loop is None:
                raise RuntimeError(message)
        return self._loop

    def _set_retry_after(self, code: int, retry_after: str) -> None:
        """
        Check the "Retry-After" header in the response and set retry_after property
        based upon it.

        Parameters:
            code (int): The HTTP response code
            retry_after (str): The value of the "Retry-After" header

        Returns:
            None.
        """
        if retry_after.isnumeric():
            retval = int(retry_after)
        else:
            try:
                retval = int(
                    datetime.datetime.strptime(
                        retry_after, "%a, %d %b %G %T %Z"
                    ).timestamp()
                )
            except ValueError:
                return
        LOG.warning(
            "Task %s received Retry-After %s due to %s",
            asyncio.current_task(),
            retval,
            self.get_http_status_description(code),
        )
        self.retry_after = int(time.time()) + retval
        try:
            fail_reason = ConnectionFailureReason(code)
        except ValueError:
            fail_reason = ConnectionFailureReason.UNKNOWN
        self._set_connection_failure_reason(fail_reason)

    async def async_query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 1,
        requires_authentication: bool = True,
    ) -> tuple[int, str | None, URL | None]:
        """
        Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query/body parameters.
                                                    Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            timeout (int, optional): timeout in seconds. Defaults to 1.
            requires_authentication (bool, optional): True if authentication is
                                                    required to perform query.
                                                    Defaults to True.
                                                    If true and authenticated flag not
                                                    set, will wait for flag to be set.

        Returns:
            tuple with integer return code, optional response text, and optional URL of
            response
        """

        async def handle_query_response(
            response: ClientResponse | None,
        ) -> tuple[int, str | None, URL | None, str | None]:
            if response is None:
                return 0, None, None, None
            response_text = await response.text()

            return (
                response.status,
                response_text,
                response.url,
                response.headers.get("Retry-After"),
            )

        if method not in ("GET", "POST"):
            raise ValueError("method must be GET or POST")
        current_time = time.time()
        if self.retry_after > current_time:
            LOG.debug(
                "Retry after set, query %s for %s waiting until %s",
                method,
                uri,
                datetime.datetime.fromtimestamp(self.retry_after),
            )
            await asyncio.sleep(self.retry_after - current_time)

        if requires_authentication and not self.authenticated_flag.is_set():
            LOG.info("%s for %s waiting for authenticated flag to be set", method, uri)
            await self._authenticated_flag.wait()
        else:
            with ADTPulseConnection._class_threadlock:
                if ADTPulseConnection._api_version == ADT_DEFAULT_VERSION:
                    await self.async_fetch_version()

        url = self.make_url(uri)
        headers = extra_headers if extra_headers is not None else {}
        if uri in ADT_HTTP_BACKGROUND_URIS:
            headers.setdefault("Accept", ADT_OTHER_HTTP_ACCEPT_HEADERS["Accept"])
        if self.detailed_debug_logging:
            LOG.debug(
                "Attempting %s %s params=%s timeout=%d",
                method,
                url,
                extra_params,
                timeout,
            )
        retry = 0
        return_value: tuple[int, str | None, URL | None, str | None] = (
            HTTPStatus.OK.value,
            None,
            None,
            None,
        )
        while retry < MAX_RETRIES:
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=extra_headers,
                    params=extra_params if method == "GET" else None,
                    data=extra_params if method == "POST" else None,
                    timeout=timeout,
                ) as response:
                    return_value = await handle_query_response(response)
                    retry += 1

                    if return_value[0] in RECOVERABLE_ERRORS:
                        LOG.info(
                            "query returned recoverable error code %s: %s,"
                            "retrying (count = %d)",
                            return_value[0],
                            self.get_http_status_description(return_value[0]),
                            retry,
                        )
                        if retry == MAX_RETRIES:
                            LOG.warning(
                                "Exceeded max retries of %d, giving up", MAX_RETRIES
                            )
                            response.raise_for_status()
                        await asyncio.sleep(2**retry + uniform(0.0, 1.0))
                        continue

                    response.raise_for_status()
                    break
            except (
                TimeoutError,
                ClientConnectionError,
                ClientConnectorError,
                ClientResponseError,
            ) as ex:
                if return_value[0] is not None and return_value[3] is not None:
                    self._set_retry_after(
                        return_value[0],
                        return_value[3],
                    )
                    break
                LOG.debug(
                    "Error %s occurred making %s request to %s",
                    ex.args,
                    method,
                    url,
                    exc_info=True,
                )
                break
        return (return_value[0], return_value[1], return_value[2])

    def query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout=1,
        requires_authentication: bool = True,
    ) -> tuple[int, str | None, URL | None]:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query/body parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            timeout (int, optional): timeout in seconds. Defaults to 1.
            requires_authentication (bool, optional): True if authentication is required
                                                    to perform query. Defaults to True.
                                                    If true and authenticated flag not
                                                    set, will wait for flag to be set.
        Returns:
            tuple with integer return code, optional response text, and optional URL of
            response
        """
        coro = self.async_query(
            uri,
            method,
            extra_params,
            extra_headers,
            timeout=timeout,
            requires_authentication=requires_authentication,
        )
        return asyncio.run_coroutine_threadsafe(
            coro, self.check_sync("Attempting to run sync query from async login")
        ).result()

    async def query_orb(self, level: int, error_message: str) -> BeautifulSoup | None:
        """Query ADT Pulse ORB.

        Args:
            level (int): error level to log on failure
            error_message (str): error message to use on failure

        Returns:
            Optional[BeautifulSoup]: A Beautiful Soup object, or None if failure
        """
        code, response, url = await self.async_query(
            ADT_ORB_URI,
            extra_headers={"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        )

        return make_soup(code, response, url, level, error_message)

    def make_url(self, uri: str) -> str:
        """Create a URL to service host from a URI.

        Args:
            uri (str): the URI to convert

        Returns:
            str: the converted string
        """
        with self._attribute_lock:
            return f"{self._api_host}{API_PREFIX}{ADTPulseConnection._api_version}{uri}"

    async def async_fetch_version(self) -> None:
        """Fetch ADT Pulse version."""
        response_path: str | None = None
        response_code = HTTPStatus.OK.value
        with ADTPulseConnection._class_threadlock:
            if ADTPulseConnection._api_version != ADT_DEFAULT_VERSION:
                return

            signin_url = f"{self.service_host}"
            try:
                async with self._session.get(
                    signin_url,
                ) as response:
                    response_code = response.status
                    # we only need the headers here, don't parse response
                    response.raise_for_status()
                    response_path = response.url.path
            except (ClientResponseError, ClientConnectionError):
                LOG.warning(
                    "Error %i: occurred during API version fetch, defaulting to %s",
                    response_code,
                    ADT_DEFAULT_VERSION,
                )
                return
            if response_path is not None:
                m = re.search("/myhome/(.+)/[a-z]*/", response_path)
                if m is not None:
                    ADTPulseConnection._api_version = m.group(1)
                    LOG.debug(
                        "Discovered ADT Pulse version %s at %s",
                        ADTPulseConnection._api_version,
                        self.service_host,
                    )
                    return

            LOG.warning(
                "Couldn't auto-detect ADT Pulse version, defaulting to %s",
                ADT_DEFAULT_VERSION,
            )

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
                self._set_connection_failure_reason(ConnectionFailureReason.UNKNOWN)
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
                    self.retry_after = int(time.time()) + retry_after
                return None
            if self.make_url(ADT_SUMMARY_URI) != str(response[2]):
                # more specifically:
                # redirect to signin.jsp = username/password error
                # redirect to mfaSignin.jsp = fingerprint error
                # locked out = error == "Sign In unsuccessful. Your account has been
                # locked after multiple sign in attempts.Try again in 30 minutes."
                LOG.error("Authentication error encountered logging into ADT Pulse")
                self._set_connection_failure_reason(
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
                self._set_connection_failure_reason(
                    ConnectionFailureReason.MFA_REQUIRED
                )
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
            self._set_connection_failure_reason(ConnectionFailureReason.UNKNOWN)
            return None
        soup = check_response(response)
        if soup is None:
            return None
        with self._attribute_lock:
            self._authenticated_flag.set()
            self._last_login_time = int(time.time())
        return soup

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
        self._authenticated_flag.clear()
