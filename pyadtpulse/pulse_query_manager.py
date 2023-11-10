"""Pulse Query Manager."""
from logging import getLogger
from asyncio import Event, current_task, sleep
from datetime import datetime
from http import HTTPStatus
from random import uniform
from re import search
from time import time

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
    ADT_DEFAULT_VERSION,
    ADT_HTTP_BACKGROUND_URIS,
    ADT_ORB_URI,
    ADT_OTHER_HTTP_ACCEPT_HEADERS,
    API_PREFIX,
    ConnectionFailureReason,
)
from .pulse_connection_info import PulseConnectionInfo
from .util import make_soup, set_debug_lock

LOG = getLogger(__name__)

RECOVERABLE_ERRORS = {
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.GATEWAY_TIMEOUT,
}
RETRY_LATER_ERRORS = {HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.TOO_MANY_REQUESTS}
MAX_RETRIES = 3


class PulseQueryManager(PulseConnectionInfo):
    """Pulse Query Manager."""

    __slots__ = (
        "_retry_after",
        "_connection_failure_reason",
        "_authenticated_flag",
        "_api_version",
        "_pcm_attribute_lock",
    )

    @staticmethod
    def _get_http_status_description(status_code: int) -> str:
        """Get HTTP status description."""
        status = HTTPStatus(status_code)
        return status.description

    def __init__(
        self,
        host: str,
        session: ClientSession | None = None,
        detailed_debug_logging: bool = False,
        debug_locks: bool = False,
    ) -> None:
        """Initialize Pulse Query Manager."""
        self._pcm_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pcm_attribute_lock"
        )
        self._retry_after = int(time())
        self._connection_failure_reason = ConnectionFailureReason.NO_FAILURE
        self._authenticated_flag = Event()
        self._api_version = ADT_DEFAULT_VERSION
        super().__init__(host, session, detailed_debug_logging, debug_locks)

    @property
    def retry_after(self) -> int:
        """Get the number of seconds to wait before retrying HTTP requests."""
        with self._pcm_attribute_lock:
            return self._retry_after

    @retry_after.setter
    def retry_after(self, seconds: int) -> None:
        """Set time after which HTTP requests can be retried.

        Raises: ValueError if seconds is less than current time.
        """
        if seconds < time():
            raise ValueError("retry_after cannot be less than current time")
        with self._pcm_attribute_lock:
            self._retry_after = seconds

    @property
    def connection_failure_reason(self) -> ConnectionFailureReason:
        """Get the connection failure reason."""
        with self._pcm_attribute_lock:
            return self._connection_failure_reason

    @connection_failure_reason.setter
    def connection_failure_reason(self, reason: ConnectionFailureReason) -> None:
        """Set the connection failure reason."""
        with self._pcm_attribute_lock:
            self._connection_failure_reason = reason

    def _compute_retry_after(self, code: int, retry_after: str) -> None:
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
                    datetime.strptime(retry_after, "%a, %d %b %G %T %Z").timestamp()
                )
            except ValueError:
                return
        LOG.warning(
            "Task %s received Retry-After %s due to %s",
            current_task(),
            retval,
            self._get_http_status_description(code),
        )
        self.retry_after = int(time()) + retval
        try:
            fail_reason = ConnectionFailureReason(code)
        except ValueError:
            fail_reason = ConnectionFailureReason.UNKNOWN
        self._connection_failure_reason = fail_reason

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
        current_time = time()
        if self.retry_after > current_time:
            LOG.debug(
                "Retry after set, query %s for %s waiting until %s",
                method,
                uri,
                datetime.fromtimestamp(self.retry_after),
            )
            await sleep(self.retry_after - current_time)

        if requires_authentication and not self._authenticated_flag.is_set():
            LOG.info("%s for %s waiting for authenticated flag to be set", method, uri)
            await self._authenticated_flag.wait()
        else:
            if self._api_version == ADT_DEFAULT_VERSION:
                await self.async_fetch_version()

        url = self.make_url(uri)
        headers = extra_headers if extra_headers is not None else {}
        if uri in ADT_HTTP_BACKGROUND_URIS:
            headers.setdefault("Accept", ADT_OTHER_HTTP_ACCEPT_HEADERS["Accept"])
        if self._detailed_debug_logging:
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
                            self._get_http_status_description(return_value[0]),
                            retry,
                        )
                        if retry == MAX_RETRIES:
                            LOG.warning(
                                "Exceeded max retries of %d, giving up", MAX_RETRIES
                            )
                            response.raise_for_status()
                        await sleep(2**retry + uniform(0.0, 1.0))
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
                    self._compute_retry_after(
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
        return f"{self._api_host}{API_PREFIX}{self._api_version}{uri}"

    async def async_fetch_version(self) -> None:
        """Fetch ADT Pulse version."""
        response_path: str | None = None
        response_code = HTTPStatus.OK.value
        if self._api_version != ADT_DEFAULT_VERSION:
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
            m = search("/myhome/(.+)/[a-z]*/", response_path)
            if m is not None:
                self._api_version = m.group(1)
                LOG.debug(
                    "Discovered ADT Pulse version %s at %s",
                    self._api_version,
                    self.service_host,
                )
                return

        LOG.warning(
            "Couldn't auto-detect ADT Pulse version, defaulting to %s",
            ADT_DEFAULT_VERSION,
        )

    @property
    def authenticated_flag(self) -> Event:
        """Get the authenticated flag."""
        with self._pcm_attribute_lock:
            return self._authenticated_flag

    @property
    def api_version(self) -> str:
        """Get the API version."""
        with self._pcm_attribute_lock:
            return self._api_version
