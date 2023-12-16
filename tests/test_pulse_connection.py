"""Test Pulse Connection."""
import pytest
from bs4 import BeautifulSoup

from pyadtpulse.const import DEFAULT_API_HOST, ConnectionFailureReason
from pyadtpulse.pulse_authentication_properties import PulseAuthenticationProperties
from pyadtpulse.pulse_connection import PulseConnection
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus


@pytest.mark.asyncio
async def test_login(mocked_server_responses, get_mocked_url, read_file, mock_sleep):
    """Test Pulse Connection."""
    s = PulseConnectionStatus()
    pcp = PulseConnectionProperties(DEFAULT_API_HOST)
    pa = PulseAuthenticationProperties(
        "test@example.com", "testpassword", "testfingerprint"
    )
    pc = PulseConnection(s, pcp, pa)
    # first call to signin post is successful in conftest.py
    result = await pc.async_do_login_query()
    assert result == BeautifulSoup(read_file("summary.html"), "html.parser")
    assert mock_sleep.call_count == 0
    assert s.authenticated_flag.is_set()
    assert pc._login_backoff.backoff_count == 0
    assert s.connection_failure_reason == ConnectionFailureReason.NO_FAILURE
    await pc.async_do_logout_query()
    assert not s.authenticated_flag.is_set()
    assert mock_sleep.call_count == 0
    assert pc._login_backoff.backoff_count == 0
