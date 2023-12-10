"""Test Pulse Query Manager."""
import time
from datetime import datetime

import pytest

from pyadtpulse.const import ADT_ORB_URI, DEFAULT_API_HOST, ConnectionFailureReason
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import PulseQueryManager


@pytest.mark.asyncio
async def test_retry_after(mocked_server_responses, mock_sleep, freeze_time_to_now):
    """Test retry after."""

    retry_after_time = 120
    now = time.time()
    retry_date = now + float(retry_after_time)
    retry_date_str = datetime.fromtimestamp(retry_date).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)

    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=429,
        headers={"Retry-After": str(retry_after_time)},
    )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=200,
    )
    mocked_server_responses.get(
        cp.make_url(ADT_ORB_URI),
        status=503,
        headers={"Retry-After": retry_date_str},
    )

    s = PulseConnectionStatus()
    cp = PulseConnectionProperties(DEFAULT_API_HOST)
    p = PulseQueryManager(s, cp)
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.TOO_MANY_REQUESTS
    assert mock_sleep.call_count == 0
    # make sure we can't override the retry
    s.get_backoff().reset_backoff()
    assert s.get_backoff().expiration_time == (now + float(retry_after_time))
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_once_with(float(retry_after_time))
    await p.async_query(ADT_ORB_URI, requires_authentication=False)
    assert s.connection_failure_reason == ConnectionFailureReason.SERVICE_UNAVAILABLE
