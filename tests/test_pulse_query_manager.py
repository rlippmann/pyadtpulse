"""Test Pulse Query Manager."""
import time
from datetime import datetime, timedelta

import pytest
from aiohttp import client_exceptions

from pyadtpulse.const import ADT_ORB_URI, DEFAULT_API_HOST, ConnectionFailureReason
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import MAX_RETRIES, PulseQueryManager


@pytest.mark.asyncio
async def test_retry_after(mocked_server_responses, mock_sleep, freeze_time_to_now):
    """Test retry after."""

    retry_after_time = 120
    frozen_time = freeze_time_to_now
    now = time.time()

    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)

    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
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
async def test_async_query_exceptions(mocked_server_responses, mock_sleep):
    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)
    timeout = 2
    curr_sleep_count = 0
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
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_args_list[0][0][0] == 2.0
    curr_sleep_count += 1
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert mock_sleep.call_count == curr_sleep_count
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    for _ in range(MAX_RETRIES + 1):
        mocked_server_responses.get(
            cp.make_url(ADT_ORB_URI),
            exception=client_exceptions.ClientConnectionError(),
        )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
    for i in range(MAX_RETRIES + 1, curr_sleep_count):
        assert mock_sleep.call_count == curr_sleep_count + MAX_RETRIES
        assert mock_sleep.call_args_list[i][0][0] == timeout ** (i - curr_sleep_count)

    assert s.connection_failure_reason == ConnectionFailureReason.CLIENT_ERROR

    assert s.get_backoff().backoff_count == 1
    backoff_sleep = s.get_backoff().get_current_backoff_interval()
    await p.async_query(ADT_ORB_URI, requires_authentication=False, timeout=timeout)
    # pqm backoff should trigger here
    curr_sleep_count += MAX_RETRIES + 2  # 1 backoff for query, 1 for connection backoff
    assert mock_sleep.call_count == curr_sleep_count
    assert mock_sleep.call_args_list[curr_sleep_count - 2][0][0] == backoff_sleep
    assert mock_sleep.call_args_list[curr_sleep_count - 1][0][0] == timeout
    assert s.get_backoff().backoff_count == 0
