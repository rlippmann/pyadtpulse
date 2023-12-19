"""Test Pulse Connection."""
import asyncio
import datetime

import pytest
from bs4 import BeautifulSoup

from pyadtpulse.const import (
    ADT_LOGIN_URI,
    ADT_MFA_FAIL_URI,
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
    assert pc._connection_status.get_backoff().backoff_count == 0
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
    assert not pc._connection_status.authenticated_flag.is_set()
    assert not pc.is_connected
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.SERVER_ERROR
    )
    await pc.async_do_login_query()
    assert pc._login_backoff.backoff_count == 0
    # 2 retries first time, 3 the second
    assert mock_sleep.call_count == MAX_RETRIES - 1 + MAX_RETRIES
    assert pc.login_in_progress is False

    assert pc._connection_status.get_backoff().backoff_count == 2
    assert not pc._connection_status.authenticated_flag.is_set()
    assert not pc.is_connected
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.SERVER_ERROR
    )
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=302,
        headers={
            "Location": get_mocked_url(ADT_SUMMARY_URI),
        },
    )
    await pc.async_do_login_query()
    # will do a backoff, then query
    assert mock_sleep.call_count == MAX_RETRIES - 1 + MAX_RETRIES + 1
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
    # shouldn't sleep at all
    assert mock_sleep.call_count == MAX_RETRIES - 1 + MAX_RETRIES + 1
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    assert pc._connection_status.authenticated_flag.is_set()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
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
    assert pc.is_connected
    assert pc._connection_status.authenticated_flag.is_set()
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI), status=200, body=read_file("signin_locked.html")
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.ACCOUNT_LOCKED
    )
    # won't sleep yet
    assert not pc.is_connected
    assert not pc._connection_status.authenticated_flag.is_set()
    # don't set backoff on locked account, just set expiration time on backoff
    assert pc._login_backoff.backoff_count == 0
    assert mock_sleep.call_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=302,
        headers={
            "Location": get_mocked_url(ADT_SUMMARY_URI),
        },
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    assert mock_sleep.call_count == 1
    assert mock_sleep.call_args_list[0][0][0] == 60 * 30
    assert pc.is_connected
    assert pc._connection_status.authenticated_flag.is_set()
    freeze_time_to_now.tick(delta=datetime.timedelta(seconds=60 * 30 + 1))
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI), status=200, body=read_file("signin_locked.html")
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.ACCOUNT_LOCKED
    )
    assert pc._login_backoff.backoff_count == 0
    assert mock_sleep.call_count == 1


@pytest.mark.asyncio
async def test_invalid_credentials(
    mocked_server_responses, mock_sleep, get_mocked_url, read_file
):
    pc = setup_pulse_connection()
    # do initial login
    await pc.async_do_login_query()
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI), status=200, body=read_file("signin_fail.html")
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.INVALID_CREDENTIALS
    )
    assert pc._login_backoff.backoff_count == 1
    assert mock_sleep.call_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI), status=200, body=read_file("signin_fail.html")
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.INVALID_CREDENTIALS
    )
    assert pc._login_backoff.backoff_count == 2
    assert mock_sleep.call_count == 1
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=302,
        headers={
            "Location": get_mocked_url(ADT_SUMMARY_URI),
        },
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    assert pc._login_backoff.backoff_count == 0
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_mfa_failure(
    mocked_server_responses, mock_sleep, get_mocked_url, read_file
):
    pc = setup_pulse_connection()
    # do initial login
    await pc.async_do_login_query()
    assert mock_sleep.call_count == 0
    assert pc.login_in_progress is False
    assert pc._login_backoff.backoff_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=307,
        headers={"Location": get_mocked_url(ADT_MFA_FAIL_URI)},
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.MFA_REQUIRED
    )
    assert pc._login_backoff.backoff_count == 1
    assert mock_sleep.call_count == 0
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=307,
        headers={"Location": get_mocked_url(ADT_MFA_FAIL_URI)},
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.MFA_REQUIRED
    )
    assert pc._login_backoff.backoff_count == 2
    assert mock_sleep.call_count == 1
    mocked_server_responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=302,
        headers={
            "Location": get_mocked_url(ADT_SUMMARY_URI),
        },
    )
    await pc.async_do_login_query()
    assert (
        pc._connection_status.connection_failure_reason
        == ConnectionFailureReason.NO_FAILURE
    )
    assert pc._login_backoff.backoff_count == 0
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_only_single_login(mocked_server_responses):
    async def login_task():
        await pc.async_do_login_query()

    pc = setup_pulse_connection()
    # delay one task for a little bit
    for i in range(4):
        pc._login_backoff.increment_backoff()
    task1 = asyncio.create_task(login_task())
    task2 = asyncio.create_task(login_task())
    await task2
    assert pc.login_in_progress
    assert not pc.is_connected
    assert not task1.done()
    await task1
    assert not pc.login_in_progress
    assert pc.is_connected
