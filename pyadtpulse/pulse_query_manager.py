"""Pulse Query Manager."""
from logging import getLogger
from asyncio import current_task
from datetime import datetime
from http import HTTPStatus
from time import time

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientError,
    ClientResponse,
    ClientResponseError,
    ServerConnectionError,
    ServerTimeoutError,
)
from bs4 import BeautifulSoup
from typeguard import typechecked
from yarl import URL

from .const import (
    ADT_HTTP_BACKGROUND_URIS,
    ADT_ORB_URI,
    ADT_OTHER_HTTP_ACCEPT_HEADERS,
    ConnectionFailureReason,
)
from .pulse_backoff import PulseBackoff
from .pulse_connection_properties import PulseConnectionProperties
from .pulse_connection_status import PulseConnectionStatus
from .util import make_soup, set_debug_lock

LOG = getLogger(__name__)

RECOVERABLE_ERRORS = {
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.GATEWAY_TIMEOUT,
}
RETRY_LATER_ERRORS = frozenset(
    {HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.TOO_MANY_REQUESTS}
)
RETRY_LATER_CONNECTION_STATUSES = frozenset(
    {
        reason
        for reason in ConnectionFailureReason
        if reason.value[0] in RETRY_LATER_ERRORS
    }
)
CHANGEABLE_CONNECTION_STATUSES = frozenset(
    RETRY_LATER_CONNECTION_STATUSES
    | {
        ConnectionFailureReason.NO_FAILURE,
        ConnectionFailureReason.CLIENT_ERROR,
        ConnectionFailureReason.SERVER_ERROR,
    }
)
MAX_RETRIES = 3


class PulseQueryManager:
    """Pulse Query Manager."""

    __slots__ = (
        "_pqm_attribute_lock",
        "_connection_properties",
        "_connection_status",
        "_debug_locks",
    )

    @staticmethod
    @typechecked
    def _get_http_status_description(status_code: int) -> str:
        """Get HTTP status description."""
        status = HTTPStatus(status_code)
        return status.description

    @typechecked
    def __init__(
        self,
        connection_status: PulseConnectionStatus,
        connection_properties: PulseConnectionProperties,
        debug_locks: bool = False,
    ) -> None:
        """Initialize Pulse Query Manager."""
        self._pqm_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pqm_attribute_lock"
        )
        self._connection_status = connection_status
        self._connection_properties = connection_properties
        self._debug_locks = debug_locks

    @staticmethod
    @typechecked
    async def _handle_query_response(
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

    @typechecked
    def _set_retry_after(self, code: int, retry_after: str) -> None:
        """
        Check the "Retry-After" header in the response and set retry_after property
        based upon it.

        Will also set the connection status failure reason and rety after
        properties.

        Parameters:
            code (int): The HTTP response code
            retry_after (str): The value of the "Retry-After" header

        Returns:
            None.
        """
        now = time()
        if retry_after.isnumeric():
            retval = float(retry_after)
        else:
            try:
                retval = datetime.strptime(
                    retry_after, "%a, %d %b %Y %H:%M:%S %Z"
                ).timestamp()
                retval -= now
            except ValueError:
                return
        description = self._get_http_status_description(code)
        LOG.warning(
            "Task %s received Retry-After %s due to %s",
            current_task(),
            retval,
            description,
        )
        # don't set the retry_after if it is in the past
        if retval > 0:
            self._connection_status.retry_after = now + retval

    @typechecked
    def _handle_http_errors(
        self, return_value: tuple[int, str | None, URL | None, str | None]
    ) -> None:
        failure_reason = ConnectionFailureReason.SERVER_ERROR
        if return_value[0] is not None and return_value[3] is not None:
            self._set_retry_after(
                return_value[0],
                return_value[3],
            )
        if return_value[0] == HTTPStatus.TOO_MANY_REQUESTS:
            failure_reason = ConnectionFailureReason.TOO_MANY_REQUESTS
        if return_value[0] == HTTPStatus.SERVICE_UNAVAILABLE:
            failure_reason = ConnectionFailureReason.SERVICE_UNAVAILABLE
        self._update_connection_status(failure_reason)

    @typechecked
    def _handle_network_errors(self, e: Exception) -> ConnectionFailureReason:
        if isinstance(e, (ServerConnectionError, ServerTimeoutError)):
            return ConnectionFailureReason.SERVER_ERROR
        if (
            isinstance(e, (ClientConnectionError))
            and "Connection refused" in str(e)
            or ("timed out") in str(e)
        ):
            return ConnectionFailureReason.SERVER_ERROR
        return ConnectionFailureReason.CLIENT_ERROR

    def _update_connection_status(
        self, failure_reason: ConnectionFailureReason
    ) -> None:
        """Update connection status.

        Will also increment or reset the backoff.
        """
        if failure_reason not in CHANGEABLE_CONNECTION_STATUSES:
            return
        if failure_reason == ConnectionFailureReason.NO_FAILURE:
            self._connection_status.reset_backoff()
        elif failure_reason not in (RETRY_LATER_CONNECTION_STATUSES):
            self._connection_status.increment_backoff()
        self._connection_status.connection_failure_reason = failure_reason

    @typechecked
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

        async def setup_query():
            if method not in ("GET", "POST"):
                raise ValueError("method must be GET or POST")
            await self._connection_status.get_backoff().wait_for_backoff()
            if (
                requires_authentication
                and not self._connection_status.authenticated_flag.is_set()
            ):
                LOG.info(
                    "%s for %s waiting for authenticated flag to be set", method, uri
                )
                await self._connection_status.authenticated_flag.wait()
            else:
                if not self._connection_properties.api_version:
                    await self.async_fetch_version()
                    if not self._connection_properties.api_version:
                        raise ValueError(
                            "Could not determine API version for connection"
                        )

        await setup_query()
        url = self._connection_properties.make_url(uri)
        headers = extra_headers if extra_headers is not None else {}
        if uri in ADT_HTTP_BACKGROUND_URIS:
            headers.setdefault("Accept", ADT_OTHER_HTTP_ACCEPT_HEADERS["Accept"])
        if self._connection_properties.detailed_debug_logging:
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
        query_backoff = PulseBackoff(
            f"Query:{method} {uri}",
            self._connection_status.get_backoff().initial_backoff_interval,
            threshold=1,
            debug_locks=self._debug_locks,
        )
        failure_reason = ConnectionFailureReason.NO_FAILURE
        while retry < MAX_RETRIES:
            try:
                await query_backoff.wait_for_backoff()
                retry += 1
                query_backoff.increment_backoff()
                async with self._connection_properties.session.request(
                    method,
                    url,
                    headers=extra_headers,
                    params=extra_params if method == "GET" else None,
                    data=extra_params if method == "POST" else None,
                    timeout=timeout,
                ) as response:
                    return_value = await self._handle_query_response(response)
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
                        continue
                    response.raise_for_status()
                    failure_reason = ConnectionFailureReason.NO_FAILURE
                    break
            except ClientResponseError:
                self._handle_http_errors(return_value)
                return (return_value[0], return_value[1], return_value[2])
            except (
                ClientConnectorError,
                ServerTimeoutError,
                ClientError,
                ServerConnectionError,
            ) as ex:
                LOG.debug(
                    "Error %s occurred making %s request to %s",
                    ex.args,
                    method,
                    url,
                    exc_info=True,
                )
                failure_reason = self._handle_network_errors(ex)
                continue

        if failure_reason != ConnectionFailureReason.NO_FAILURE:
            LOG.debug("Exceeded max retries of %d, giving up", MAX_RETRIES)
        self._update_connection_status(failure_reason)
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

    async def async_fetch_version(self) -> None:
        """Fetch ADT Pulse version.

        Exceptions are passed through to the caller since if this fails, there is
        probably some underlying connection issue.
        """
        response_values: tuple[int, str | None, URL | None, str | None] = (
            HTTPStatus.OK.value,
            None,
            None,
            None,
        )
        if self._connection_properties.api_version:
            return

        signin_url = self._connection_properties.service_host
        try:
            async with self._connection_properties.session.get(
                signin_url,
            ) as response:
                response_values = await self._handle_query_response(response)
                response.raise_for_status()

        except ClientResponseError as ex:
            LOG.debug(
                "Error %s occurred determining Pulse API version",
                ex.args,
                exc_info=True,
            )
            self._handle_http_errors(response_values)
            return
        except (
            ClientConnectorError,
            ServerTimeoutError,
            ClientError,
            ServerConnectionError,
        ) as ex:
            LOG.debug(
                "Error %s occurred determining Pulse API version",
                ex.args,
                exc_info=True,
            )
            self._update_connection_status(self._handle_network_errors(ex))
            return
        version = self._connection_properties.get_api_version(str(response_values[2]))
        if version is not None:
            self._connection_properties.api_version = version
            LOG.debug(
                "Discovered ADT Pulse version %s at %s",
                self._connection_properties.api_version,
                self._connection_properties.service_host,
            )
            if (
                self._connection_status.connection_failure_reason
                != ConnectionFailureReason.NO_FAILURE
            ):
                self._update_connection_status(ConnectionFailureReason.NO_FAILURE)
            return
