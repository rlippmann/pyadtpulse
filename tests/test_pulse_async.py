"""Test Pulse Query Manager."""
import asyncio
import re
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from conftest import LoginType, add_custom_response, add_signin
from pyadtpulse.const import (
    ADT_DEVICE_URI,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    ADT_SUMMARY_URI,
    ADT_SYNC_CHECK_URI,
    ADT_TIMEOUT_URI,
    DEFAULT_API_HOST,
)
from pyadtpulse.exceptions import PulseNotLoggedInError
from pyadtpulse.pyadtpulse_async import PyADTPulseAsync

DEFAULT_SYNC_CHECK = "234532-456432-0"
NEXT_SYNC_CHECK = "234533-456432-0"


def set_keepalive(get_mocked_url, mocked_server_responses, repeat: bool = False):
    m = mocked_server_responses
    m.post(
        get_mocked_url(ADT_TIMEOUT_URI),
        body="",
        content_type="text/html",
        repeat=repeat,
    )


@pytest.mark.asyncio
async def test_mocked_responses(
    read_file,
    mocked_server_responses,
    get_mocked_mapped_static_responses,
    get_mocked_url,
    extract_ids_from_data_directory,
):
    """Fixture to test mocked responses."""
    static_responses = get_mocked_mapped_static_responses
    m = mocked_server_responses
    async with aiohttp.ClientSession() as session:
        for url, file_name in static_responses.items():
            # Make an HTTP request to the URL
            response = await session.get(url)

            # Assert the status code is 200
            assert response.status == 200

            # Assert the content matches the content of the file
            expected_content = read_file(file_name)
            actual_content = await response.text()
            assert actual_content == expected_content
        devices = extract_ids_from_data_directory
        for device_id in devices:
            response = await session.get(
                f"{get_mocked_url(ADT_DEVICE_URI)}?id={device_id}"
            )
            assert response.status == 200
            expected_content = read_file(f"device_{device_id}.html")
            actual_content = await response.text()
            assert actual_content == expected_content

        # redirects
        add_custom_response(
            mocked_server_responses,
            get_mocked_url,
            read_file,
            ADT_LOGIN_URI,
            file_name="signin.html",
        )
        response = await session.get(f"{DEFAULT_API_HOST}/", allow_redirects=True)
        assert response.status == 200
        actual_content = await response.text()
        expected_content = read_file("signin.html")
        assert actual_content == expected_content
        add_custom_response(
            mocked_server_responses,
            get_mocked_url,
            read_file,
            ADT_LOGIN_URI,
            file_name="signin.html",
        )
        response = await session.get(
            get_mocked_url(ADT_LOGOUT_URI), allow_redirects=True
        )
        assert response.status == 200
        expected_content = read_file("signin.html")
        actual_content = await response.text()
        assert actual_content == expected_content
        add_signin(
            LoginType.SUCCESS, mocked_server_responses, get_mocked_url, read_file
        )
        response = await session.post(
            get_mocked_url(ADT_LOGIN_URI), allow_redirects=True
        )
        assert response.status == 200
        expected_content = read_file(static_responses[get_mocked_url(ADT_SUMMARY_URI)])
        actual_content = await response.text()
        assert actual_content == expected_content
        pattern = re.compile(rf"{re.escape(get_mocked_url(ADT_SYNC_CHECK_URI))}/?.*$")
        m.get(pattern, status=200, body="1-0-0", content_type="text/html")
        response = await session.get(
            get_mocked_url(ADT_SYNC_CHECK_URI), params={"ts": "first call"}
        )
        assert response.status == 200
        actual_content = await response.text()
        expected_content = "1-0-0"
        assert actual_content == expected_content
        set_keepalive(get_mocked_url, m)
        response = await session.post(get_mocked_url(ADT_TIMEOUT_URI))


# not sure we need this
@pytest.fixture
def wrap_wait_for_update():
    with patch.object(
        PyADTPulseAsync,
        "wait_for_update",
        new_callable=AsyncMock,
        spec=PyADTPulseAsync,
        wraps=PyADTPulseAsync.wait_for_update,
    ) as wait_for_update:
        yield wait_for_update


@pytest.fixture
@pytest.mark.asyncio
async def adt_pulse_instance(mocked_server_responses, extract_ids_from_data_directory):
    p = PyADTPulseAsync("testuser@example.com", "testpassword", "testfingerprint")
    await p.async_login()
    # Assertions after login
    assert p.site.name == "Robert Lippmann"
    assert p._timeout_task is not None
    assert p._timeout_task.get_name() == p._get_timeout_task_name()
    assert p._sync_task is None
    assert p.site.zones_as_dict is not None
    assert len(p.site.zones_as_dict) == len(extract_ids_from_data_directory) - 3
    return p, mocked_server_responses


@pytest.mark.asyncio
async def test_login(adt_pulse_instance, extract_ids_from_data_directory):
    """Fixture to test login."""
    p, _ = await adt_pulse_instance
    # make sure everything is there on logout
    await p.async_logout()
    await asyncio.sleep(1)
    assert p.site.name == "Robert Lippmann"
    assert p.site.zones_as_dict is not None
    assert len(p.site.zones_as_dict) == len(extract_ids_from_data_directory) - 3
    assert p._timeout_task is None


async def do_wait_for_update(p: PyADTPulseAsync, shutdown_event: asyncio.Event):
    while not shutdown_event.is_set():
        try:
            await p.wait_for_update()
        except asyncio.CancelledError:
            break


@pytest.mark.asyncio
async def test_wait_for_update(adt_pulse_instance, get_mocked_url):
    p, responses = await adt_pulse_instance
    shutdown_event = asyncio.Event()
    task = asyncio.create_task(do_wait_for_update(p, shutdown_event))
    await asyncio.sleep(1)
    while task.get_stack is None:
        await asyncio.sleep(1)
    await p.async_logout()
    assert p._sync_task is None
    assert p.site.name == "Robert Lippmann"
    with pytest.raises(PulseNotLoggedInError):
        await task

    # test exceptions
    # check we can't wait for update if not logged in
    with pytest.raises(PulseNotLoggedInError):
        await p.wait_for_update()

    responses.post(
        get_mocked_url(ADT_LOGIN_URI),
        status=307,
        headers={"Location": get_mocked_url(ADT_SUMMARY_URI)},
    )
    await p.async_login()


@pytest.mark.asyncio
async def test_orb_update(mocked_server_responses, get_mocked_url, read_file):
    response = mocked_server_responses
    pattern = re.compile(rf"{re.escape(get_mocked_url(ADT_SYNC_CHECK_URI))}/?.*$")

    def signal_status_change():
        response.get(
            pattern,
            body=DEFAULT_SYNC_CHECK,
            content_type="text/html",
        )
        response.get(pattern, body="1-0-0", content_type="text/html")
        response.get(pattern, body="2-0-0", content_type="text/html")
        response.get(
            pattern,
            body=NEXT_SYNC_CHECK,
            content_type="text/html",
        )
        response.get(
            pattern,
            body=NEXT_SYNC_CHECK,
            content_type="text/html",
        )

    def open_patio():
        response.get(
            get_mocked_url(ADT_ORB_URI),
            body=read_file("orb_patio_opened.html"),
            content_type="text/html",
        )
        signal_status_change()

    def close_patio():
        response.get(
            get_mocked_url(ADT_ORB_URI),
            body=read_file("orb.html"),
            content_type="text/html",
        )
        signal_status_change()

    def open_garage():
        response.get(
            get_mocked_url(ADT_ORB_URI),
            body=read_file("orb_garage.html"),
            content_type="text/html",
        )
        signal_status_change()

    def open_both_garage_and_patio():
        response.get(
            get_mocked_url(ADT_ORB_URI),
            body=read_file("orb_patio_garage.html"),
            content_type="text/html",
        )
        signal_status_change()

    def setup_sync_check():
        open_patio()
        close_patio()

    async def test_sync_check_and_orb():
        code, content, _ = await p._pulse_connection.async_query(
            ADT_ORB_URI, requires_authentication=False
        )
        assert code == 200
        assert content == read_file("orb_patio_opened.html")
        await asyncio.sleep(1)
        code, content, _ = await p._pulse_connection.async_query(
            ADT_ORB_URI, requires_authentication=False
        )
        assert code == 200
        assert content == read_file("orb.html")
        await asyncio.sleep(1)
        for _ in range(1):
            code, content, _ = await p._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, requires_authentication=False
            )
            assert code == 200
            assert content == DEFAULT_SYNC_CHECK
            code, content, _ = await p._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, requires_authentication=False
            )
            assert code == 200
            assert content == "1-0-0"
            code, content, _ = await p._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, requires_authentication=False
            )
            assert code == 200
            assert content == "2-0-0"
            code, content, _ = await p._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, requires_authentication=False
            )
            assert code == 200
            assert content == NEXT_SYNC_CHECK
            code, content, _ = await p._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, requires_authentication=False
            )
            assert code == 200
            assert content == NEXT_SYNC_CHECK

    p = PyADTPulseAsync("testuser@example.com", "testpassword", "testfingerprint")
    shutdown_event = asyncio.Event()
    shutdown_event.clear()
    setup_sync_check()
    # do a first run though to make sure aioresponses will work ok
    await test_sync_check_and_orb()
    open_patio()
    await p.async_login()
    task = asyncio.create_task(do_wait_for_update(p, shutdown_event))
    await asyncio.sleep(3)
    assert p._sync_task is not None
    shutdown_event.set()
    task.cancel()
    await task
    await p.async_logout()
    assert len(p.site.zones) == 13
    assert p.site.zones_as_dict[11].state == "Open"
    assert p._sync_task is None
    # assert m.call_count == 2


@pytest.mark.asyncio
async def test_keepalive_check(mocked_server_responses):
    p = PyADTPulseAsync("testuser@example.com", "testpassword", "testfingerprint")
    await p.async_login()
    assert p._timeout_task is not None
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_infinite_sync_check(mocked_server_responses, get_mocked_url):
    p = PyADTPulseAsync("testuser@example.com", "testpassword", "testfingerprint")
    pattern = re.compile(rf"{re.escape(get_mocked_url(ADT_SYNC_CHECK_URI))}/?.*$")
    mocked_server_responses.get(
        pattern,
        body=DEFAULT_SYNC_CHECK,
        content_type="text/html",
        repeat=True,
    )
    shutdown_event = asyncio.Event()
    shutdown_event.clear()
    await p.async_login()
    task = asyncio.create_task(do_wait_for_update(p, shutdown_event))
    await asyncio.sleep(5)
    shutdown_event.set()
    task.cancel()
    await task
