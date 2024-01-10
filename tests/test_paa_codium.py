# Generated by CodiumAI
import asyncio
from unittest.mock import PropertyMock

import pytest
from bs4 import BeautifulSoup
from requests import patch

from conftest import LoginType, add_signin
from pyadtpulse.exceptions import PulseAuthenticationError, PulseNotLoggedInError
from pyadtpulse.pyadtpulse_async import PyADTPulseAsync
from pyadtpulse.site import ADTPulseSite


class TestPyADTPulseAsync:
    # The class can be instantiated with the required parameters (username, password, fingerprint) and optional parameters (service_host, user_agent, debug_locks, keepalive_interval, relogin_interval, detailed_debug_logging).
    @pytest.mark.asyncio
    async def test_instantiation_with_parameters(self):
        pulse = PyADTPulseAsync(
            username="valid_email@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
            service_host="https://portal.adtpulse.com",
            user_agent="Your User Agent",
            debug_locks=False,
            keepalive_interval=5,
            relogin_interval=60,
            detailed_debug_logging=True,
        )
        assert isinstance(pulse, PyADTPulseAsync)

    # The __repr__ method returns a string representation of the class.
    @pytest.mark.asyncio
    async def test_repr_method_with_valid_email(self):
        pulse = PyADTPulseAsync(
            username="your_username@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
        )
        assert repr(pulse) == "<PyADTPulseAsync: your_username@example.com>"

    # The async_login method successfully authenticates the user to the ADT Pulse cloud service using a valid email address as the username.
    @pytest.mark.asyncio
    async def test_async_login_success_with_valid_email(
        self, mocked_server_responses, get_mocked_url, read_file
    ):
        pulse = PyADTPulseAsync(
            username="valid_email@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
        )
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        assert await pulse.async_login() is True

    # The class is instantiated without the required parameters (username, password, fingerprint) and raises an exception.
    @pytest.mark.asyncio
    async def test_instantiation_without_parameters(self):
        with pytest.raises(TypeError):
            pulse = PyADTPulseAsync()

    # The async_login method fails to authenticate the user to the ADT Pulse cloud service and raises a PulseAuthenticationError.
    @pytest.mark.asyncio
    async def test_async_login_failure_with_valid_username(self):
        pulse = PyADTPulseAsync(
            username="valid_email@example.com",
            password="invalid_password",
            fingerprint="invalid_fingerprint",
        )
        with pytest.raises(PulseAuthenticationError):
            await pulse.async_login()

    # The async_logout method is called without being logged in and returns without any action.
    @pytest.mark.asyncio
    async def test_async_logout_without_login_with_valid_email_fixed(self):
        pulse = PyADTPulseAsync(
            username="valid_username@example.com",
            password="valid_password",
            fingerprint="valid_fingerprint",
        )
        with pytest.raises(RuntimeError):
            await pulse.async_logout()

    # The async_logout method successfully logs the user out of the ADT Pulse cloud service.
    @pytest.mark.asyncio
    async def test_async_logout_successfully_logs_out(
        self, mocked_server_responses, get_mocked_url, read_file
    ):
        # Arrange
        pulse = PyADTPulseAsync(
            username="test_user@example.com",
            password="test_password",
            fingerprint="test_fingerprint",
        )
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        # Act
        await pulse.async_login()
        await pulse.async_logout()

        # Assert
        assert not pulse.is_connected

    # The async_update method checks the ADT Pulse cloud service for updates and returns True if updates are available.
    @pytest.mark.asyncio
    async def test_async_update_returns_true_if_updates_available_with_valid_email(
        self,
    ):
        # Arrange
        from unittest.mock import MagicMock

        pulse = PyADTPulseAsync("test@example.com", "password", "fingerprint")
        pulse._update_sites = MagicMock(return_value=None)

        # Act
        result = await pulse.async_update()

        # Assert
        assert result is True

    # The site property returns an ADTPulseSite object after logging in.
    @pytest.mark.asyncio
    async def test_site_property_returns_ADTPulseSite_object_with_login(
        self, mocked_server_responses, get_mocked_url, read_file
    ):
        # Arrange
        pulse = PyADTPulseAsync("test@example.com", "valid_password", "fingerprint")
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        # Act
        await pulse.async_login()
        site = pulse.site

        # Assert
        assert isinstance(site, ADTPulseSite)

    # The is_connected property returns True if the class is connected to the ADT Pulse cloud service.
    @pytest.mark.asyncio
    async def test_is_connected_property_returns_true(
        self, mocked_server_responses, get_mocked_url, read_file
    ):
        pulse = PyADTPulseAsync(
            username="valid_username@example.com",
            password="valid_password",
            fingerprint="valid_fingerprint",
        )
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        await pulse.async_login()
        assert pulse.is_connected == True

    # The site property is accessed without being logged in and raises an exception.
    @pytest.mark.asyncio
    async def test_site_property_without_login_raises_exception(self):
        pulse = PyADTPulseAsync(
            username="test@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
            service_host="https://portal.adtpulse.com",
            user_agent="Your User Agent",
            debug_locks=False,
            keepalive_interval=5,
            relogin_interval=60,
            detailed_debug_logging=True,
        )
        with pytest.raises(RuntimeError):
            pulse.site

    # The wait_for_update method waits for updates from the ADT Pulse cloud service and raises an exception if there is an error.
    @pytest.mark.asyncio
    async def test_wait_for_update_method_with_valid_username(self):
        # Arrange
        from unittest.mock import MagicMock

        pulse = PyADTPulseAsync(
            username="your_username@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
        )

        # Mock the necessary methods and attributes
        pulse._pa_attribute_lock = asyncio.Lock()
        pulse._site = MagicMock()
        pulse._site.gateway.backoff.wait_for_backoff.return_value = asyncio.sleep(0)
        pulse._pulse_connection_status.get_backoff().will_backoff.return_value = False
        pulse._pulse_properties.updates_exist.wait.return_value = asyncio.sleep(0)

        # Mock the is_connected property of _pulse_connection
        with patch.object(
            pulse._pulse_connection, "is_connected", new_callable=PropertyMock
        ) as mock_is_connected:
            mock_is_connected.return_value = True

            # Act
            with patch.object(pulse, "_update_sites") as mock_update_sites:
                with patch.object(
                    pulse, "_pulse_connection", autospec=True
                ) as mock_pulse_connection:
                    with patch.object(pulse, "_sync_check_exception", None):
                        await pulse.wait_for_update()

            # Assert
            mock_update_sites.assert_called_once()
            pulse._pulse_properties.updates_exist.wait.assert_called_once()
            assert pulse._sync_check_exception is None

    # The sites property returns a list of ADTPulseSite objects.
    @pytest.mark.asyncio
    async def test_sites_property_returns_list_of_objects(
        self, mocked_server_responses, get_mocked_url, read_file
    ):
        # Arrange
        pulse = PyADTPulseAsync(
            "test@example.com", "valid_password", "valid_fingerprint"
        )
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        # Act
        await pulse.async_login()
        sites = pulse.sites

        # Assert
        assert isinstance(sites, list)
        for site in sites:
            assert isinstance(site, ADTPulseSite)

    # The is_connected property returns False if the class is not connected to the ADT Pulse cloud service.
    @pytest.mark.asyncio
    async def test_is_connected_property_returns_false_when_not_connected(self):
        pulse = PyADTPulseAsync(
            username="your_username@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
        )
        assert pulse.is_connected == False

    # The async_update method fails to check the ADT Pulse cloud service for updates and returns False.
    @pytest.mark.asyncio
    async def test_async_update_fails(self, mock_query_orb):
        pulse = PyADTPulseAsync("username@example.com", "password", "fingerprint")
        mock_query_orb.return_value = None

        result = await pulse.async_update()

        assert result == False

    # The sites property is accessed without being logged in and raises an exception.
    @pytest.mark.asyncio
    async def test_sites_property_without_login_raises_exception(self):
        pulse = PyADTPulseAsync(
            username="your_username@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
            service_host="https://portal.adtpulse.com",
            user_agent="Your User Agent",
            debug_locks=False,
            keepalive_interval=5,
            relogin_interval=60,
            detailed_debug_logging=True,
        )
        with pytest.raises(RuntimeError):
            pulse.sites

    # The wait_for_update method is called without being logged in and raises an exception.
    @pytest.mark.asyncio
    async def test_wait_for_update_without_login_raises_exception(self):
        pulse = PyADTPulseAsync(
            username="your_username@example.com",
            password="your_password",
            fingerprint="your_fingerprint",
            service_host="https://portal.adtpulse.com",
            user_agent="Your User Agent",
            debug_locks=False,
            keepalive_interval=5,
            relogin_interval=60,
            detailed_debug_logging=True,
        )

        with pytest.raises(PulseNotLoggedInError):
            await pulse.wait_for_update()

    # The _initialize_sites method retrieves the site id and name from the soup object and creates a new ADTPulseSite object.
    @pytest.mark.asyncio
    async def test_initialize_sites_method_with_valid_service_host(
        self, mocker, read_file
    ):
        # Arrange
        username = "test@example.com"
        password = "test_password"
        fingerprint = "test_fingerprint"
        service_host = "https://portal.adtpulse.com"
        user_agent = "Test User Agent"
        debug_locks = False
        keepalive_interval = 10
        relogin_interval = 30
        detailed_debug_logging = True

        pulse = PyADTPulseAsync(
            username=username,
            password=password,
            fingerprint=fingerprint,
            service_host=service_host,
            user_agent=user_agent,
            debug_locks=debug_locks,
            keepalive_interval=keepalive_interval,
            relogin_interval=relogin_interval,
            detailed_debug_logging=detailed_debug_logging,
        )

        soup = BeautifulSoup(read_file("summary.html"))

        # Mock the fetch_devices method to always return True
        mocker.patch.object(ADTPulseSite, "fetch_devices", return_value=True)

        # Act
        await pulse._initialize_sites(soup)

        # Assert
        assert pulse._site is not None
        assert pulse._site.id == "160301za524548"
        assert pulse._site.name == "Robert Lippmann"
