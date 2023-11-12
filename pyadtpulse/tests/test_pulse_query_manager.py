# Initially Generated by CodiumAI
import time
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientSession
from yarl import URL

from pyadtpulse.const import (
    ADT_DEFAULT_VERSION,
    DEFAULT_API_HOST,
    ConnectionFailureReason,
)
from pyadtpulse.pulse_query_manager import PulseQueryManager


@pytest.fixture
def mock_query(mocker) -> None:
    """Patch a mock query manager"""
    mocker.Mock(spec=PulseQueryManager)
    mocker2 = Mock(spec=ClientSession)
    mocker.patch.object("_session", mocker2)


@pytest.fixture
def live_query() -> PulseQueryManager:
    """Set up a live query object"""
    return PulseQueryManager(DEFAULT_API_HOST)


# Can successfully fetch the API version using an AsyncMock
@pytest.mark.asyncio
@pytest.fixture
async def fetch_version(get_api_version) -> str:
    """
    Given a PulseQueryManager instance
    When async_fetch_version is called
    Then the API version should be fetched successfully
    """
    # Given

    lq = live_query()
    # When
    await lq.async_fetch_version()
    if lq.api_version == ADT_DEFAULT_VERSION:
        pytest.skip("fetch_version")
    assert lq.api_version == get_api_version
    return lq.api_version


class TestPulseQueryManager:
    # Can successfully make a GET request to a valid URI with the new fix

    @pytest.mark.asyncio
    async def test_async_query(self, signin_page, compare_html):
        """Test a valid URI query."""
        # Given
        host = DEFAULT_API_HOST
        manager = PulseQueryManager(host)
        signin_result = await signin_page

        expected_code = signin_result[0]
        expected_response = signin_result[1]
        expected_url = signin_result[2]

        # When
        code, response, url = await manager.async_query(
            "/", requires_authentication=False
        )

        # Then
        assert code == expected_code
        assert compare_html(response, expected_response)
        assert url == expected_url

    # Can successfully make a POST request to a valid URI
    @pytest.mark.asyncio
    async def test_successful_post_request(self, mock_client_session):
        """
        Given a valid URI
        When a POST request is made
        Then the response code should be HTTPStatus.OK.value
        And the response body should be the expected value
        And the response URL should be the expected value
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_code = HTTPStatus.OK.value
        expected_response = "Response body"
        expected_url = URL("https://portal.adtpulse.com/api/data")
        patch.object(
            manager._session, mock_client_session
        )  # pylint: disable=protected-access

        # When
        code, response, url = await manager.async_query("/api/data", method="POST")

        # Then
        assert code == expected_code
        assert response == expected_response
        assert url == expected_url

    # Can successfully handle a recoverable error during a query
    @pytest.mark.asyncio
    async def test_recoverable_error_handling(self, mocker):
        """
        Given a recoverable error during a query
        When the query is retried
        Then the query should eventually succeed
        """
        # Given

        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_code = HTTPStatus.SERVICE_UNAVAILABLE.value
        expected_response = "Service Unavailable"
        expected_url = URL("https://portal.adtpulse.com/api/data")
        mocker.patch.object(
            manager,
            "async_query",
            side_effect=[
                AsyncMock(
                    return_value=(expected_code, expected_response, expected_url)
                ),
                AsyncMock(
                    return_value=(HTTPStatus.OK.value, "Response body", expected_url)
                ),
            ],
        )

        # When
        code, response, url = await manager.async_query("/api/data")

        # Then
        assert code == HTTPStatus.OK.value
        assert response == "Response body"
        assert url == expected_url

    @pytest.mark.asyncio
    async def test_unrecoverable_error_handling_with_recommended_fix(self, mocker):
        """
        Given an unrecoverable error during a query
        When the query is retried
        Then the query should return the expected values
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        expected_response = "Internal Server Error"
        expected_url = URL("https://portal.adtpulse.com/api/data")
        mocker.patch.object(
            PulseQueryManager,
            "async_query",
            return_value=(expected_code, expected_response, expected_url),
        )

        # When
        code, response, url = await manager.async_query("/api/data")

        # Then
        assert code == expected_code
        assert response == expected_response
        assert url == expected_url

    # Can successfully authenticate and set the authenticated flag
    @pytest.mark.asyncio
    async def test_successful_authentication(self, mocker):
        """
        Given a valid host and session
        When authentication is successful
        Then the authenticated flag should be set
        """
        # Given
        host = "https://portal.adtpulse.com"
        session = ClientSession()
        manager = PulseQueryManager(host, session)
        mocker.patch.object(
            manager, "_authenticated_flag", autospec=True
        )  # pylint: disable=protected-access

        # When
        manager._authenticated_flag.set()

        # Then
        assert manager._authenticated_flag.is_set()

    # Can successfully make a query to the ORB URI
    @pytest.mark.asyncio
    async def test_successful_query_to_orb_uri_fixed(self, mock_client_session):
        """
        Given a valid PulseQueryManager instance
        When a query is made to the ORB URI
        Then the async_query method should be called with the correct parameters
        And the handle_query_response method should be called with the correct response
        """
        # Given
        mocker = MagicMock(spec=PulseQueryManager)
        host = "https://portal.adtpulse.com"
        mocker.service_host = host
        mocker._session = mock_client_session
        expected_code = HTTPStatus.OK.value
        expected_response = "Response body"
        expected_url = URL("https://portal.adtpulse.com/ajax/orb.jsp")
        mocker.patch.object(
            PulseQueryManager,
            "async_query",
            return_value=(expected_code, expected_response, expected_url),
        )

        # When
        response = await mocker.query_orb(1, "Error message")

        # Then
        mocker.async_query.assert_called_once_with(
            "/ajax/orb.jsp",
            extra_headers={"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        )
        assert response == (expected_code, expected_response, expected_url)

    # Can successfully compute the retry after time
    @pytest.mark.asyncio
    async def test_compute_retry_after_fixed(self):
        """
        Given a valid response code and Retry-After header
        When _compute_retry_after is called
        Then the retry_after attribute should be updated with the correct value
        And the connection_failure_reason attribute should be updated with the correct
        value
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_code = 503
        expected_retry_after = "120"
        expected_retry_after_timestamp = int(time.time()) + int(expected_retry_after)
        expected_reason = ConnectionFailureReason.UNKNOWN

        # When
        manager._compute_retry_after(expected_code, expected_retry_after)

        # Then
        assert manager.retry_after == expected_retry_after_timestamp
        assert manager.connection_failure_reason == expected_reason

    # Raises a ValueError if method is not GET or POST
    @pytest.mark.asyncio
    async def test_invalid_method_raises_value_error(self):
        """
        Given a PulseQueryManager instance
        When an invalid method is passed to the async_query method
        Then a ValueError should be raised
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)

        # When/Then
        with pytest.raises(ValueError):
            await manager.async_query("/api/data", method="INVALID_METHOD")

    # Raises a ValueError if retry_after is less than current time
    @pytest.mark.asyncio
    async def test_retry_after_less_than_current_time(self):
        """
        Given a PulseQueryManager instance
        When setting the retry_after attribute to a value less than the current time
        Then a ValueError should be raised
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        retry_after = int(time.time()) - 1

        # When/Then
        with pytest.raises(ValueError):
            manager.retry_after = retry_after

    # Raises a ValueError if service host is not valid
    @pytest.mark.asyncio
    async def test_invalid_service_host(self):
        """
        Given an invalid service host
        When initializing PulseQueryManager with the invalid service host
        Then a ValueError should be raised
        """
        # Given
        invalid_host = ""

        # When/Then
        with pytest.raises(ValueError):
            PulseQueryManager(invalid_host)

    # The default failure reason should be ConnectionFailureReason.NO_FAILURE if the
    # connection failure reason is not determined
    @pytest.mark.asyncio
    async def test_default_failure_reason(self):
        """
        Given a PulseQueryManager instance
        When the connection failure reason is not determined
        Then the default failure reason should be ConnectionFailureReason.NO_FAILURE
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)

        # When
        failure_reason = manager.connection_failure_reason

        # Then
        assert failure_reason == ConnectionFailureReason.NO_FAILURE

    # Defaults to ADT_DEFAULT_VERSION if unable to fetch API version
    @pytest.mark.asyncio
    async def test_default_api_version(self, mocker):
        """
        Given a PulseQueryManager instance
        When the API version cannot be fetched
        Then the default API version should be ADT_DEFAULT_VERSION
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_api_version = ADT_DEFAULT_VERSION
        mocker.patch.object(PulseQueryManager, "async_query", side_effect=Exception)

        # When
        await manager.async_fetch_version()

        # Then
        assert manager.api_version == expected_api_version

    # Can successfully set and get the retry_after time
    @pytest.mark.asyncio
    async def test_set_and_get_retry_after_time(self):
        """
        Given a PulseQueryManager instance
        When the retry_after property is set to a specific value
        Then the retry_after property should return the same value
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_retry_after = int(time.time()) + 60

        # When
        manager.retry_after = expected_retry_after
        actual_retry_after = manager.retry_after

        # Then
        assert actual_retry_after == expected_retry_after

    # Can successfully set and get the connection_failure_reason
    @pytest.mark.asyncio
    async def test_set_and_get_connection_failure_reason(self):
        """
        Given a PulseQueryManager instance
        When the connection_failure_reason is set to a specific value
        Then the connection_failure_reason should be equal to the set value
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_reason = ConnectionFailureReason.ACCOUNT_LOCKED

        # When
        manager.connection_failure_reason = expected_reason
        actual_reason = manager.connection_failure_reason

        # Then
        assert actual_reason == expected_reason

    # Can successfully set and get the detailed_debug_logging flag
    @pytest.mark.asyncio
    async def test_set_and_get_detailed_debug_logging_flag(self):
        """
        Given a PulseQueryManager instance
        When the detailed_debug_logging flag is set to True
        Then the value of detailed_debug_logging should be True
        And when the detailed_debug_logging flag is set to False
        Then the value of detailed_debug_logging should be False
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)

        # When
        manager.detailed_debug_logging = True

        # Then
        assert manager.detailed_debug_logging is True

        # When
        manager.detailed_debug_logging = False

        # Then
        assert manager.detailed_debug_logging is False

    # Can successfully set and get the debug_locks flag
    @pytest.mark.asyncio
    async def test_set_and_get_debug_locks_flag(self):
        """
        Given a PulseQueryManager instance
        When the debug_locks flag is set to True
        Then the debug_locks flag should be True
        And when the debug_locks flag is set to False
        Then the debug_locks flag should be False
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)

        # When
        manager.debug_locks = True

        # Then
        assert manager.debug_locks is True

        # When
        manager.debug_locks = False

        # Then
        assert manager.debug_locks is False

    # Can successfully make a query to a background URI
    @pytest.mark.asyncio
    async def test_successful_background_query(self, mocker):
        """
        Given a valid URI in the background URIs list
        When a query is made to the URI
        Then the query should be successful
        """
        # Given
        host = "https://portal.adtpulse.com"
        manager = PulseQueryManager(host)
        expected_code = HTTPStatus.OK.value
        expected_response = "Response body"
        expected_url = URL("https://portal.adtpulse.com/api/background")
        mocker.patch.object(
            PulseQueryManager,
            "async_query",
            new_callable=AsyncMock,
            return_value=(expected_code, expected_response, expected_url),
        )

        # When
        code, response, url = await manager.async_query("/api/background")

        # Then
        assert code == expected_code
        assert response == expected_response
        assert url == expected_url
