"""ADT Pulse connection. End users should probably not call this directly."""

import logging
import asyncio
import re
from random import uniform
from threading import Lock, RLock
from time import time

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from bs4 import BeautifulSoup

from .const import (
    ADT_DEFAULT_HTTP_HEADERS,
    ADT_DEFAULT_VERSION,
    ADT_HTTP_REFERER_URIS,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    API_PREFIX,
)
from .util import DebugRLock, close_response, handle_response, make_soup

RECOVERABLE_ERRORS = [429, 500, 502, 503, 504]
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

    def check_sync(self, message: str) -> asyncio.AbstractEventLoop:
        """Checks if sync login was performed.

        Returns the loop to use for run_coroutine_threadsafe if so.
        Raises RuntimeError with given message if not."""
        with self._attribute_lock:
            if self._loop is None:
                raise RuntimeError(message)
        return self._loop

    async def async_query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 1,
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

        Returns:
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                    None on failure
                                    ClientResponse will already be closed.
        """
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
                    await response.text()

                    if response.status in RECOVERABLE_ERRORS:
                        retry += 1
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
                    retry = 4  # success, break loop
            except (
                TimeoutError,
                ClientConnectionError,
                ClientConnectorError,
                ClientResponseError,
            ) as ex:
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
    ) -> ClientResponse | None:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            timeout (int, optional): timeout in seconds. Defaults to 1.
        Returns:
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                      None on failure
                                      ClientResponse will already be closed.
        """
        coro = self.async_query(uri, method, extra_params, extra_headers, timeout)
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
            self._last_login_time = int(time())
        return retval

    async def async_do_logout_query(self, site_id: str | None) -> None:
        """Performs a logout query to the ADT Pulse site."""
        params = {}
        if site_id is not None:
            params.update({"network": site_id})
        params.update({"partner": "adt"})
        await self.async_query(ADT_LOGOUT_URI, extra_params=params, timeout=10)
