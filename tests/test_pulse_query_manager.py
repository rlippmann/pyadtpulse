"""Test Pulse Query Manager."""
import logging
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from aiohttp import client_exceptions
from bs4 import BeautifulSoup

from conftest import MOCKED_API_VERSION
from pyadtpulse.const import ADT_ORB_URI, DEFAULT_API_HOST, ConnectionFailureReason
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import MAX_RETRIES, PulseQueryManager


@pytest.mark.asyncio
async def test_fetch_version(mocked_server_responses):
    """Test fetch version."""
    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)
    await p.async_fetch_version()
    assert cp.api_version == MOCKED_API_VERSION


@pytest.mark.asyncio
async def test_query_orb(
    mocked_server_responses, read_file, mock_sleep, get_mocked_connection_properties
):
    """Test query orb.

    We also check that it waits for authenticated flag.
    """

    async def query_orb_task():
        return await p.query_orb(logging.DEBUG, "Failed to query orb")

    s = PulseConnectionStatus()
    cp = get_mocked_connection_properties
    p = PulseQueryManager(s, cp)
    orb_file = read_file("orb.html")
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI), status=200, content_type="text/html", body=orb_file
    )
    task = asyncio.create_task(query_orb_task())
    await asyncio.sleep(2)
    assert not task.done()
    s.authenticated_flag.set()
    await task
    assert task.done()
    assert task.result() == BeautifulSoup(orb_file, "html.parser")
    assert mock_sleep.call_count == 1  # from the asyncio.sleep call above
    mocked_server_responses.get(cp.make_url(ADT_ORB_URI), status=404)
    result = await query_orb_task()
    assert result is None
    assert mock_sleep.call_count == 1
    assert s.connection_failure_reason == ConnectionFailureReason.SERVER_ERROR
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI), status=200, content_type="text/html", body=orb_file
    )
    result = await query_orb_task()
    assert result == BeautifulSoup(orb_file, "html.parser")
    assert mock_sleep.call_count == 1
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE


@pytest.mark.asyncio
async def test_retry_after(
    mocked_server_responses,
    mock_sleep,
    freeze_time_to_now,
    get_mocked_connection_properties,
):
    """Test retry after."""

    retry_after_time = 120
    frozen_time = freeze_time_to_now
    now = time.time()

    s = PulseConnectionStatus()
    cp = get_mocked_connection_properties
    p = PulseQueryManager(s, cp)

    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=429,
        headers={"Retry-After": str(retry_after_time)},
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.TOO_MANY_REQUESTS
    assert mock_sleep.call_count == 0
    # make sure we can't override the retry
    s.get_backoff().reset_backoff()
    assert s.get_backoff().expiration_time == (now + float(retry_after_time))
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_once_with(float(retry_after_time))
    frozen_time.tick(timedelta(seconds=retry_after_time + 1))
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    # shouldn't sleep since past expiration time
    assert mock_sleep.call_count == 1
    frozen_time.tick(timedelta(seconds=1))
    now = time.time()
    retry_date = now + float(retry_after_time)
    retry_date_str = datetime.fromtimestamp(retry_date).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    # need to get the new retry after time since it doesn't have fractions of seconds
    new_retry_after = (
        datetime.strptime(retry_date_str, "%a, %d %b %Y %H:%M:%S GMT").timestamp() - now
    )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
        headers={"Retry-After": retry_date_str},
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.SERVICE_UNAVAILABLE
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[1][0][0] == new_retry_after
    frozen_time.tick(timedelta(seconds=retry_after_time + 1))
    # unavailable with no retry after
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.SERVICE_UNAVAILABLE
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_count == 2
    # retry after in the past
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
        headers={"Retry-After": retry_date_str},
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.SERVICE_UNAVAILABLE
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_async_query_exceptions(
    mocked_server_responses, mock_sleep, get_mocked_connection_properties
):
    s = PulseConnectionStatus()
    cp = get_mocked_connection_properties
    p = PulseQueryManager(s, cp)
    timeout = 3
    curr_sleep_count = 0
    # test one exception
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        exception=client_exceptions.ClientConnectionError(),
    )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
    assert mock_sleep.call_count == 1
    curr_sleep_count = mock_sleep.call_count
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_args_list[0][0][0] == timeout
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == curr_sleep_count
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    # need to do ClientConnectorError, but it requires initialization
    for ex in (
        client_exceptions.ClientConnectionError(),
        client_exceptions.ClientError(),
        client_exceptions.ClientOSError(),
        client_exceptions.ServerDisconnectedError(),
        client_exceptions.ServerTimeoutError(),
        client_exceptions.ServerConnectionError(),
    ):
        if type(ex) in (
            client_exceptions.ClientConnectionError,
            client_exceptions.ClientError,
            client_exceptions.ClientOSError,
        ):
            error_type = ConnectionFailureReason.CLIENT_ERROR
        else:
            error_type = ConnectionFailureReason.SERVER_ERROR
        for _ in range(MAX_RETRIES + 1):
            mocked_server_responses.get(
                cp.make_url(ADT_ORB_URI),
                exception=ex,
            )
        mocked_server_responses.get(
            cp.make_url(ADT_ORB_URI),
            status=200,
        )
        await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
        assert (
            mock_sleep.call_count == curr_sleep_count + MAX_RETRIES
        ), f"Failure on exception {type(ex).__name__}"
        for i in range(curr_sleep_count + 1, curr_sleep_count + MAX_RETRIES):
            assert mock_sleep.call_args_list[i][0][0] == timeout * (
                2 ** (i - curr_sleep_count - 1)
            ), f"Failure on query {i}, exception {ex}"

        assert (
            s.connection_failure_reason == error_type
        ), f"Error type failure on exception {type(ex).__name__}"

        assert s.get_backoff().backoff_count == 1
        backoff_sleep = s.get_backoff().get_current_backoff_interval()
        await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
        # pqm backoff should trigger here
        curr_sleep_count += (
            MAX_RETRIES + 2
        )  # 1 backoff for query, 1 for connection backoff
        assert mock_sleep.call_count == curr_sleep_count
        assert mock_sleep.call_args_list[curr_sleep_count - 2][0][0] == backoff_sleep
        assert mock_sleep.call_args_list[curr_sleep_count - 1][0][0] == timeout
        assert s.get_backoff().backoff_count == 0
        mocked_server_responses.get(
            cp.make_url(ADT_ORB_URI),
            status=200,
        )
        assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
        # this shouldn't trigger a sleep
        await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
        assert mock_sleep.call_count == curr_sleep_count
