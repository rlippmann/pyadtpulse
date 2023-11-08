"""ADT Pulse connection. End users should probably not call this directly."""

import logging
import asyncio
import datetime
import re
import time
from random import uniform
from threading import Lock, RLock
from urllib.parse import quote

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
)
from .util import DebugRLock, handle_response, make_soup

RECOVERABLE_ERRORS = [500, 502, 504]
LOG = logging.getLogger(__name__)

MAX_RETRIES = 3


class ADTPulseConnection:
    """ADT Pulse connection related attributes."""

    _api_version = ADT_DEFAULT_VERSION
    _class_threadlock = Lock()

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

    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        user_agent: str = ADT_DEFAULT_HTTP_USER_AGENT["User-Agent"],
        debug_locks: bool = False,
        detailed_debug_logging: bool = False,
    ):
        """Initialize ADT Pulse connection."""
        self.check_service_host(host)
        self._api_host = host
        self._allocated_session = False
        self._authenticated_flag = asyncio.Event()
        if session is None:
            self._allocated_session = True
            self._session = ClientSession()
        else:
            self._session = session
        self._session.headers.update({"User-Agent": user_agent})
        self._session.headers.update(ADT_DEFAULT_HTTP_ACCEPT_HEADERS)
        self._attribute_lock: RLock | DebugRLock
        self._last_login_time: int = 0
        if not debug_locks:
            self._attribute_lock = RLock()
        else:
            self._attribute_lock = DebugRLock("ADTPulseConnection._attribute_lock")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._retry_after = int(time.time())
        self._detailed_debug_logging = detailed_debug_logging
        self._site_id = ""

    def __del__(self):
        """Destructor for ADTPulseConnection."""
        if self._allocated_session and self._session is not None:
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

    def check_sync(self, message: str) -> asyncio.AbstractEventLoop:
        """Checks if sync login was performed.

        Returns the loop to use for run_coroutine_threadsafe if so.
        Raises RuntimeError with given message if not."""
        with self._attribute_lock:
            if self._loop is None:
                raise RuntimeError(message)
        return self._loop

    def _set_retry_after(self, response: ClientResponse) -> None:
        """
        Check the "Retry-After" header in the response and set retry_after property
        based upon it.

        Parameters:
            response (ClientResponse): The response object.

        Returns:
            None.
        """
        header_value = response.headers.get("Retry-After")
        if header_value is None:
            return
        reason = "Unknown"
        if response.status == 429:
            reason = "Too many requests"
        elif response.status == 503:
            reason = "Service unavailable"
        if header_value.isnumeric():
            retval = int(header_value)
        else:
            try:
                retval = int(
                    datetime.datetime.strptime(
                        header_value, "%a, %d %b %G %T %Z"
                    ).timestamp()
                )
            except ValueError:
                return
        LOG.warning(
            "Task %s received Retry-After %s due to %s",
            asyncio.current_task(),
            retval,
            reason,
        )
        self.retry_after = int(time.time()) + retval

    async def async_query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        timeout: int = 1,
        requires_authentication: bool = True,
    ) -> tuple[int, str | None, URL | None]:
        """
        Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters.
                                                    Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            data (Optional[Dict], optional): data to send. Defaults to None.
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
        response_text: str | None = None
        return_code: int = 200
        response_url: URL | None = None
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
                "Attempting %s %s params=%s data=%s timeout=%d",
                method,
                url,
                extra_params,
                data,
                timeout,
            )
        retry = 0
        response: ClientResponse | None = None
        while retry < MAX_RETRIES:
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=extra_headers,
                    params=extra_params,
                    data=data if method == "POST" else None,
                    timeout=timeout,
                ) as response:
                    retry += 1
                    response_text = await response.text()
                    return_code = response.status
                    response_url = response.url

                    if response.status in RECOVERABLE_ERRORS:
                        LOG.info(
                            "query returned recoverable error code %s, "
                            "retrying (count = %d)",
                            response.status,
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
                if response and response.status in (429, 503):
                    self._set_retry_after(response)
                    response = None
                    break
                LOG.debug(
                    "Error %s occurred making %s request to %s, retrying",
                    ex.args,
                    method,
                    url,
                    exc_info=True,
                )
            finally:
                await self._session.close()

        return (return_code, response_text, response_url)

    def query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        timeout=1,
        requires_authentication: bool = True,
    ) -> tuple[int, str | None, URL | None]:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            data (Optional[Dict], optional): data to send. Defaults to None.
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
            data=data,
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
        code, response, url = await self.async_query(ADT_ORB_URI)

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
        with ADTPulseConnection._class_threadlock:
            if ADTPulseConnection._api_version != ADT_DEFAULT_VERSION:
                return

            signin_url = f"{self.service_host}/myhome{ADT_LOGIN_URI}"
            try:
                async with self._session.get(signin_url) as response:
                    # we only need the headers here, don't parse response
                    response.raise_for_status()
                    response_path = response.url.path
            except (ClientResponseError, ClientConnectionError):
                LOG.warning(
                    "Error occurred during API version fetch, defaulting to %s",
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
            soup: Optional[BeautifulSoup]: A BeautifulSoup object containing summary.jsp,
            or None if failure
        Raises:
            ValueError: if login parameters are not correct
        """
        response: tuple[int, str | None, URL | None] = (200, None, None)
        self.check_login_parameters(username, password, fingerprint)
        try:
            response = await self.async_query(
                ADT_LOGIN_URI,
                method="POST",
                extra_params={
                    "e": "ns",
                    "partner": "adt",
                },
                data={
                    "usernameForm": quote(username),
                    "passwordForm": quote(password),
                    "networkid": self._site_id,
                    "fingerprint": quote(fingerprint),
                },
                timeout=timeout,
                requires_authentication=False,
            )
        except Exception as e:  # pylint: disable=broad-except
            LOG.error("Could not log into Pulse site: %s", e)
            return None
        if not handle_response(
            response[0],
            response[2],
            logging.ERROR,
            "Error encountered communicating with Pulse site on login",
        ):
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
            LOG.error("Error logging into pulse: %s", error.get_text())
            return None
        if self.make_url(ADT_SUMMARY_URI) != str(response[2]):
            # more specifically:
            # redirect to signin.jsp = username/password error
            # redirect to mfaSignin.jsp = fingerprint error
            # locked out = error == "Sign In unsuccessful. Your account has been locked
            # after multiple sign in attempts.Try again in 30 minutes."
            LOG.error("Authentication error encountered logging into ADT Pulse")
            return None

        error = soup.find("div", "responsiveContainer")
        if error:
            LOG.error(
                "2FA authentiation required for ADT pulse username %s: %s",
                username,
                error,
            )
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
