"""Test Pulse Query Manager."""
import logging
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from aiohttp import client_exceptions
from bs4 import BeautifulSoup

from conftest import MOCKED_API_VERSION
from pyadtpulse.const import ADT_ORB_URI, DEFAULT_API_HOST
from pyadtpulse.exceptions import (
    PulseClientConnectionError,
    PulseServerConnectionError,
    PulseServiceTemporarilyUnavailableError,
)
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
async def test_fetch_version_fail(mock_server_down):
    """Test fetch version."""
    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)
    with pytest.raises(PulseServerConnectionError):
        await p.async_fetch_version()
    assert s.get_backoff().backoff_count == 1
    with pytest.raises(PulseServerConnectionError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.get_backoff().backoff_count == 2
    assert s.get_backoff().get_current_backoff_interval() == 2.0


@pytest.mark.asyncio
async def test_fetch_version_eventually_succeeds(mock_server_temporarily_down):
    """Test fetch version."""
    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)
    with pytest.raises(PulseServerConnectionError):
        await p.async_fetch_version()
    assert s.get_backoff().backoff_count == 1
    with pytest.raises(PulseServerConnectionError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.get_backoff().backoff_count == 2
    assert s.get_backoff().get_current_backoff_interval() == 2.0
    await p.async_fetch_version()
    assert s.get_backoff().backoff_count == 0


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
    with pytest.raises(PulseServerConnectionError):
        result = await query_orb_task()
    assert mock_sleep.call_count == 1
    assert s.get_backoff().backoff_count == 1
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI), status=200, content_type="text/html", body=orb_file
    )
    result = await query_orb_task()
    assert result == BeautifulSoup(orb_file, "html.parser")
    assert mock_sleep.call_count == 2


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
    with pytest.raises(PulseServiceTemporarilyUnavailableError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 0
    # make sure we can't override the retry
    s.get_backoff().reset_backoff()
    assert s.get_backoff().expiration_time == (now + float(retry_after_time))
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_once_with(float(retry_after_time))
    frozen_time.tick(timedelta(seconds=retry_after_time + 1))
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
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
    with pytest.raises(PulseServiceTemporarilyUnavailableError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[1][0][0] == new_retry_after
    frozen_time.tick(timedelta(seconds=retry_after_time + 1))
    # unavailable with no retry after
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
    )
    with pytest.raises(PulseServiceTemporarilyUnavailableError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 3
    # retry after in the past
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
        headers={"Retry-After": retry_date_str},
    )
    with pytest.raises(PulseServiceTemporarilyUnavailableError):
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 4


@pytest.mark.asyncio
async def test_async_query_exceptions(
    mocked_server_responses, mock_sleep, get_mocked_connection_properties
):
    s = PulseConnectionStatus()
    cp = get_mocked_connection_properties
    p = PulseQueryManager(s, cp)
    # test one exception
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        exception=client_exceptions.ClientConnectionError(),
    )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 1
    curr_sleep_count = mock_sleep.call_count
    assert (
        mock_sleep.call_args_list[0][0][0] == s.get_backoff().initial_backoff_interval
    )
    assert s.get_backoff().backoff_count == 0
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == 1
    assert s.get_backoff().backoff_count == 0
    query_backoff = s.get_backoff().initial_backoff_interval
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
            error_type = PulseClientConnectionError
        else:
            error_type = PulseServerConnectionError
        for _ in range(MAX_RETRIES + 1):
            mocked_server_responses.get(
                cp.make_url(ADT_ORB_URI),
                exception=ex,
            )
        mocked_server_responses.get(
            cp.make_url(ADT_ORB_URI),
            status=200,
        )
        with pytest.raises(error_type):
            await p.async_query(
                ADT_ORB_URI,
                requires_authentication=False,
            )
        # only MAX_RETRIES - 1 sleeps since first call won't sleep
        assert (
            mock_sleep.call_count == curr_sleep_count + MAX_RETRIES - 1
        ), f"Failure on exception {type(ex).__name__}"
        assert mock_sleep.call_args_list[curr_sleep_count][0][0] == query_backoff
        for i in range(curr_sleep_count + 2, curr_sleep_count + MAX_RETRIES - 1):
            assert mock_sleep.call_args_list[i][0][0] == query_backoff * 2 ** (
                i - curr_sleep_count
            ), f"Failure on exception sleep count {i} on exception {type(ex).__name__}"
        curr_sleep_count += MAX_RETRIES - 1
        assert (
            s.get_backoff().backoff_count == 1
        ), f"Failure on exception {type(ex).__name__}"
        backoff_sleep = s.get_backoff().get_current_backoff_interval()
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
        # pqm backoff should trigger here
        curr_sleep_count += 2
        # 1 backoff for query, 1 for connection backoff
        assert mock_sleep.call_count == curr_sleep_count
        assert mock_sleep.call_args_list[curr_sleep_count - 2][0][0] == backoff_sleep
        assert mock_sleep.call_args_list[curr_sleep_count - 1][0][0] == backoff_sleep
        assert s.get_backoff().backoff_count == 0
        mocked_server_responses.get(
            cp.make_url(ADT_ORB_URI),
            status=200,
        )
        # this shouldn't trigger a sleep
        await p.async_query(ADT_ORB_URI, requires_authentication=False)
        assert mock_sleep.call_count == curr_sleep_count
