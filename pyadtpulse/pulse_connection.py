"""ADT Pulse connection. End users should probably not call this directly."""

import logging
import asyncio
import datetime
import re
import time
from random import uniform
from threading import Lock, RLock

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from bs4 import BeautifulSoup

from pyadtpulse.const import (
    ADT_DEFAULT_HTTP_HEADERS,
    ADT_DEFAULT_VERSION,
    ADT_HTTP_REFERER_URIS,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    API_PREFIX,
)
from pyadtpulse.util import DebugRLock, close_response, handle_response, make_soup

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
    )

    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        user_agent: str = ADT_DEFAULT_HTTP_HEADERS["User-Agent"],
        debug_locks: bool = False,
    ):
        """Initialize ADT Pulse connection."""
        self._api_host = host
        self._allocated_session = False
        self._authenticated_flag = asyncio.Event()
        if session is None:
            self._allocated_session = True
            self._session = ClientSession()
        else:
            self._session = session
        self._session.headers.update({"User-Agent": user_agent})
        self._attribute_lock: RLock | DebugRLock
        self._last_login_time: int = 0
        if not debug_locks:
            self._attribute_lock = RLock()
        else:
            self._attribute_lock = DebugRLock("ADTPulseConnection._attribute_lock")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._retry_after = int(time.time())

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
        timeout: int = 1,
        requires_authentication: bool = True,
    ) -> ClientResponse | None:
        """
        Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters.
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
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                    None on failure
                                    ClientResponse will already be closed.
        """
        current_time = time.time()
        if self.retry_after > current_time:
            LOG.debug(
                "Retry after set, query %s for %s waiting until %s",
                method,
                uri,
                datetime.datetime.fromtimestamp(self.retry_after),
            )
            await asyncio.sleep(self.retry_after - current_time)

        if requires_authentication:
            LOG.info("%s for %s waiting for authenticated flag to be set", method, uri)
            await self._authenticated_flag.wait()
        else:
            with ADTPulseConnection._class_threadlock:
                if ADTPulseConnection._api_version == ADT_DEFAULT_VERSION:
                    await self.async_fetch_version()

        url = self.make_url(uri)

        headers = {"Accept": ADT_DEFAULT_HTTP_HEADERS["Accept"]}
        if uri not in ADT_HTTP_REFERER_URIS:
            headers["Accept"] = "*/*"

        self._session.headers.update(headers)

        LOG.debug(
            "Attempting %s %s params=%s timeout=%d", method, url, extra_params, timeout
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
                    data=extra_params if method == "POST" else None,
                    timeout=timeout,
                ) as response:
                    retry += 1
                    await response.text()

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
                    close_response(response)
                    response = None
                    break
                LOG.debug(
                    "Error %s occurred making %s request to %s, retrying",
                    ex.args,
                    method,
                    url,
                    exc_info=True,
                )

        return response

    def query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout=1,
        requires_authentication: bool = True,
    ) -> ClientResponse | None:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            timeout (int, optional): timeout in seconds. Defaults to 1.
            requires_authentication (bool, optional): True if authentication is required
                                                    to perform query. Defaults to True.
                                                    If true and authenticated flag not
                                                    set, will wait for flag to be set.
        Returns:
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                      None on failure
                                      ClientResponse will already be closed.
        """
        coro = self.async_query(
            uri, method, extra_params, extra_headers, timeout, requires_authentication
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
        response = await self.async_query(ADT_ORB_URI)

        return await make_soup(response, level, error_message)

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
        response: ClientResponse | None = None
        with ADTPulseConnection._class_threadlock:
            if ADTPulseConnection._api_version != ADT_DEFAULT_VERSION:
                return

            signin_url = f"{self.service_host}/myhome{ADT_LOGIN_URI}"
            try:
                async with self._session.get(signin_url) as response:
                    # we only need the headers here, don't parse response
                    response.raise_for_status()
            except (ClientResponseError, ClientConnectionError):
                LOG.warning(
                    "Error occurred during API version fetch, defaulting to %s",
                    ADT_DEFAULT_VERSION,
                )
                return
            if response is not None:
                m = re.search("/myhome/(.+)/[a-z]*/", response.real_url.path)
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
    ) -> ClientResponse | None:
        """
        Performs a login query to the Pulse site.

        Args:
            timeout (int, optional): The timeout value for the query in seconds.
            Defaults to 30.

        Returns:
            ClientResponse | None: The response from the query or None if the login
            was unsuccessful.
        """
        try:
            retval = await self.async_query(
                ADT_LOGIN_URI,
                method="POST",
                extra_params={
                    "partner": "adt",
                    "e": "ns",
                    "usernameForm": username,
                    "passwordForm": password,
                    "fingerprint": fingerprint,
                    "sun": "yes",
                },
                timeout=timeout,
                requires_authentication=False,
            )
        except Exception as e:  # pylint: disable=broad-except
            LOG.error("Could not log into Pulse site: %s", e)
            return None
        if retval is None:
            LOG.error("Could not log into Pulse site.")
            return None
        if not handle_response(
            retval,
            logging.ERROR,
            "Error encountered communicating with Pulse site on login",
        ):
            close_response(retval)
            return None
        with self._attribute_lock:
            self._authenticated_flag.set()
            self._last_login_time = int(time.time())
        return retval

    async def async_do_logout_query(self, site_id: str | None) -> None:
        """Performs a logout query to the ADT Pulse site."""
        params = {}
        if site_id is not None:
            params.update({"network": site_id})
        params.update({"partner": "adt"})
        await self.async_query(
            ADT_LOGOUT_URI,
            extra_params=params,
            timeout=10,
            requires_authentication=False,
        )
        self._authenticated_flag.clear()
