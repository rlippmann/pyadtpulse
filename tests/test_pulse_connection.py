"""Test Pulse Connection."""
import pytest
from bs4 import BeautifulSoup

from pyadtpulse.const import (
    ADT_LOGIN_URI,
    ADT_SUMMARY_URI,
    DEFAULT_API_HOST,
    ConnectionFailureReason,
)
from pyadtpulse.pulse_authentication_properties import PulseAuthenticationProperties
from pyadtpulse.pulse_connection import PulseConnection
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import MAX_RETRIES


def setup_pulse_connection() -> PulseConnection:
    s = PulseConnectionStatus()
    pcp = PulseConnectionProperties(DEFAULT_API_HOST)
    pa = PulseAuthenticationProperties(
        "test@example.com", "testpassword", "testfingerprint"
    )
    pc = PulseConnection(s, pcp, pa)
    return pc


@pytest.mark.asyncio
async def test_login(mocked_server_responses, get_mocked_url, read_file, mock_sleep):
    """Test Pulse Connection."""
    pc = setup_pulse_connection()
    # first call to signin post is successful in conftest.py
    result = await pc.async_do_login_query()
    assert result == BeautifulSoup(read_file("summary.html"), "html.parser")
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert pc._connection_status.authenticated_flag.is_set()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    await pc.async_do_logout_query()
    assert not pc._connection_status.authenticated_flag.is_set()
    assert mock_sleep.call_count == 0
    assert pc._login_backoff.backoff_count == 0


@pytest.mark.asyncio
async def test_login_failure_server_down(mock_server_down):
    pc = setup_pulse_connection()
    result = await pc.async_do_login_query()
    assert result is None
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.SERVER_ERROR
    )


@pytest.mark.asyncio
async def test_multiple_login(
    mocked_server_responses, get_mocked_url, read_file, mock_sleep
):
    """Test Pulse Connection."""
    pc = setup_pulse_connection()
    # first call to signin post is successful in conftest.py
    result = await pc.async_do_login_query()
    assert result == BeautifulSoup(read_file("summary.html"), "html.parser")
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert pc._connection_status.authenticated_flag.is_set()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=302,
        headers={
            "Location": get_mocked_url(ADT_SUMMARY_URI),
        },
    )
    await pc.async_do_login_query()
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert pc._connection_status.authenticated_flag.is_set()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    # this should fail
    await pc.async_do_login_query()
    assert mock_sleep.call_count == MAX_RETRIES - 1
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert pc._connection_status.get_backoff().backoff_count == 1
    assert pc._connection_status.authenticated_flag.is_set()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.SERVER_ERROR
    )


@pytest.mark.asyncio
async def test_account_lockout(
    mocked_server_responses, mock_sleep, get_mocked_url, freeze_time_to_now, read_file
):
    pc = setup_pulse_connection()
    # do initial login
    await pc.async_do_login_query()
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI), 200, read_file("summary.html")
    )
