"""Test Pulse Query Manager."""
import inspect
from datetime import datetime, timedelta
from unittest import mock

import freezegun
import pytest

from pyadtpulse.const import ADT_ORB_URI, DEFAULT_API_HOST, ConnectionFailureReason
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import PulseQueryManager


def get_calling_function() -> str | None:
    curr_frame = inspect.currentframe()
    if not curr_frame:
        return None
    if not curr_frame.f_back:
        return None
    if not curr_frame.f_back.f_back:
        return None
    if not curr_frame.f_back.f_back.f_back:
        return None
    if not curr_frame.f_back.f_back.f_back.f_back:
        return None
    frame = curr_frame.f_back.f_back.f_back.f_back
    calling_function = frame.f_globals["__name__"] + "." + frame.f_code.co_name
    return calling_function


@pytest.mark.asyncio
async def test_retry_after(mocked_server_responses):
    """Test retry after."""
    sleep_function_calls: list[str] = []

    def msleep(*args, **kwargs):
        func = get_calling_function()
        if func:
            sleep_function_calls.append(func)

    retry_after_time = 120

    now = datetime.now()
    retry_date = now + timedelta(seconds=retry_after_time * 2)
    retry_date_str = retry_date.strftime("%a, %d %b %Y %H:%M:%S GMT")
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
        status=503,
        headers={"Retry-After": retry_date_str},
    )
    with freezegun.freeze_time(now + timedelta(seconds=5)):
        with mock.patch("asyncio.sleep", side_effect=msleep) as mock_sleep:
            await p.async_query(ADT_ORB_URI, requires_authentication=False)
            assert (
                s.connection_failure_reason == ConnectionFailureReason.TOO_MANY_REQUESTS
            )
            assert mock_sleep.call_count == 1
            assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
            await p.async_query(ADT_ORB_URI, requires_authentication=False)
            assert (
                s.connection_failure_reason
                == ConnectionFailureReason.SERVICE_UNAVAILABLE
            )
            assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
