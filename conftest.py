"""Pulse Test Configuration."""
import os
import re
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib import parse

import pytest
from aiohttp import web
from aioresponses import aioresponses

# Get the root directory of your project
project_root = Path(__file__).resolve().parent

# Modify sys.path to include the project root
sys.path.insert(0, str(project_root))
test_file_dir = project_root / "tests" / "data_files"
# pylint: disable=wrong-import-position
# ruff: noqa: E402
# flake8: noqa: E402
from pyadtpulse.const import (
    ADT_DEVICE_URI,
    ADT_GATEWAY_URI,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    ADT_SUMMARY_URI,
    ADT_SYNC_CHECK_URI,
    ADT_SYSTEM_SETTINGS,
    ADT_SYSTEM_URI,
    DEFAULT_API_HOST,
)
from pyadtpulse.pulse_connection_properties import PulseConnectionProperties
from pyadtpulse.pulse_connection_status import PulseConnectionStatus
from pyadtpulse.pulse_query_manager import PulseQueryManager
from pyadtpulse.util import remove_prefix


@pytest.fixture
def read_file():
    """Fixture to read a file.

    Args:
        file_name (str): Name of the file to read
    """

    def _read_file(file_name: str) -> str:
        file_path = test_file_dir / file_name
        return file_path.read_text(encoding="utf-8")

    return _read_file


@pytest.fixture(scope="session")
@pytest.mark.asyncio
async def get_api_version() -> AsyncGenerator[str, Any]:
    """Fixture to get the API version."""
    pcp = PulseConnectionProperties(DEFAULT_API_HOST)
    pcs = PulseConnectionStatus()
    pqm = PulseQueryManager(pcs, pcp)
    await pqm.async_fetch_version()
    yield pcp.api_version


@pytest.fixture
def patched_async_query_sleep() -> Generator[AsyncMock, Any, Any]:
    """Fixture to patch asyncio.sleep in async_query()."""
    a = AsyncMock()
    with patch(
        "pyadtpulse.pulse_query_manager.async_query.asyncio.sleep", side_effect=a
    ) as mock:
        yield mock


@pytest.fixture(scope="session")
def get_mocked_api_version() -> str:
    """Fixture to get the test API version."""
    return "26.0.0-32"


@pytest.fixture
def get_mocked_connection_properties() -> PulseConnectionProperties:
    """Fixture to get the test connection properties."""
    return PulseConnectionProperties(DEFAULT_API_HOST)


@pytest.fixture
def get_mocked_url(get_mocked_connection_properties):
    def _get_mocked_url(path: str) -> str:
        return get_mocked_connection_properties.make_url(path)

    return _get_mocked_url


@pytest.fixture
def get_relative_mocked_url(get_mocked_connection_properties):
    def _get_relative_mocked_url(path: str) -> str:
        return remove_prefix(
            get_mocked_connection_properties.make_url(path), DEFAULT_API_HOST
        )

    return _get_relative_mocked_url


@pytest.fixture
def get_mocked_mapped_static_responses(get_mocked_url) -> dict[str, str]:
    """Fixture to get the test mapped responses."""
    return {
        get_mocked_url(ADT_LOGIN_URI): "signin.html",
        get_mocked_url(ADT_SUMMARY_URI): "summary.html",
        get_mocked_url(ADT_SYSTEM_URI): "system.html",
        get_mocked_url(ADT_GATEWAY_URI): "gateway.html",
    }


@pytest.fixture
def extract_ids_from_data_directory() -> list[str]:
    """Extract the device ids all the device files in the data directory."""
    id_pattern = re.compile(r"device_(\d{1,})\.html")
    ids = set()
    for file_name in os.listdir(test_file_dir):
        match = id_pattern.match(file_name)
        if match:
            ids.add(match.group(1))
    return list(ids)


@pytest.fixture
def get_default_sync_check() -> str:
    return "234532-456432-0"


@pytest.fixture
def mocked_server_responses(
    get_mocked_mapped_static_responses: dict[str, str],
    read_file,
    get_mocked_url,
    extract_ids_from_data_directory: list[str],
) -> Generator[aioresponses, Any, None]:
    """Fixture to get the test mapped responses."""
    static_responses = get_mocked_mapped_static_responses
    with aioresponses() as responses:
        for url, file_name in static_responses.items():
            responses.get(
                url, body=read_file(file_name), content_type="text/html", repeat=True
            )

            # device id rewriting
        for device_id in extract_ids_from_data_directory:
            responses.get(
                f"{get_mocked_url(ADT_DEVICE_URI)}?id={device_id}",
                body=read_file(f"device_{device_id}.html"),
                content_type="text/html",
            )
        # redirects
        responses.get(
            DEFAULT_API_HOST,
            status=302,
            headers={"Location": get_mocked_url(ADT_LOGIN_URI)},
            repeat=True,
        )
        responses.get(
            f"{DEFAULT_API_HOST}/",
            status=302,
            headers={"Location": get_mocked_url(ADT_LOGIN_URI)},
            repeat=True,
        )
        responses.get(
            f"{DEFAULT_API_HOST}/{ADT_LOGIN_URI}",
            status=307,
            headers={"Location": get_mocked_url(ADT_LOGIN_URI)},
            repeat=True,
        )
        # login/logout
        responses.post(
            get_mocked_url(ADT_LOGIN_URI),
            status=302,
            headers={
                "Location": get_mocked_url(ADT_SUMMARY_URI),
            },
        )
        responses.get(
            get_mocked_url(ADT_LOGOUT_URI),
            status=302,
            headers={"Location": get_mocked_url(ADT_LOGIN_URI)},
            repeat=True,
        )
        # not doing default sync check response or keepalive
        # because we need to set it on each test
        yield responses


@pytest.fixture
def patched_sync_task_sleep() -> Generator[AsyncMock, Any, Any]:
    """Fixture to patch asyncio.sleep in async_query()."""
    a = AsyncMock()
    with patch(
        "pyadtpulse.PyADTPulseAsync._sync_task.asyncio.sleep", side_effect=a
    ) as mock:
        yield mock


class PulseMockedWebServer:
    """Mocked Pulse Web Server."""

    def __init__(self, pulse_properties: PulseConnectionProperties):
        """Initialize the PulseMockedWebServer."""
        self.app = web.Application()
        self.logged_in = False
        self.status_code = 200
        self.retry_after_header: str | None = None
        self.pcp = pulse_properties
        self.uri_mapping: dict[str, list[str]] = {
            "/": ["signin.html"],
            self._make_local_prefix(ADT_LOGIN_URI): ["signin.html"],
            self._make_local_prefix(ADT_LOGOUT_URI): ["signout.html"],
            self._make_local_prefix(ADT_SUMMARY_URI): ["summary.html"],
            self._make_local_prefix(ADT_SYSTEM_URI): ["system.html"],
            self._make_local_prefix(ADT_SYNC_CHECK_URI): ["sync_check.html"],
            self._make_local_prefix(ADT_ORB_URI): ["orb.html"],
            self._make_local_prefix(ADT_SYSTEM_SETTINGS): ["system_settings.html"],
        }
        super().__init__()
        self.app.router.add_route("*", "/{path_info:.*}", self.handler)

    def _make_local_prefix(self, uri: str) -> str:
        return remove_prefix(self.pcp.make_url(uri), "https://")

    async def handler(self, request: web.Request) -> web.Response | web.FileResponse:
        """Handler for the PulseMockedWebServer."""
        path = request.path

        # Check if there is a query parameter for retry_after
        query_params = parse.parse_qs(request.query_string)
        retry_after_param = query_params.get("retry_after")

        def serve_file(filename: str) -> web.Response | web.FileResponse:
            try:
                return web.FileResponse(filename)
            except FileNotFoundError:
                return web.Response(text="Not found", status=404)

        # Function to parse the retry_after parameter
        def parse_retry_after(value) -> int | datetime:
            try:
                return int(value)
            except ValueError:
                return datetime.fromisoformat(value)

        # Simulate service unavailable for a specific path
        def handle_service_unavailable(path: str) -> web.Response | None:
            if path == "/service_unavailable" or self.status_code == 503:
                retry_after = retry_after_param[0] if retry_after_param else None
                self.retry_after_header = str(parse_retry_after(retry_after))
                self.status_code = 503
                return web.Response(
                    text="Service Unavailable",
                    status=self.status_code,
                    headers={"Retry-After": self.retry_after_header}
                    if self.retry_after_header
                    else None,
                )
            return None

        def handle_rate_limit_exceeded(path: str) -> web.Response | None:
            # Simulate rate limit exceeded for a specific path
            if path == "/rate_limit_exceeded" or self.status_code == 429:
                retry_after = retry_after_param[0] if retry_after_param else None
                self.retry_after_header = str(parse_retry_after(retry_after))
                self.status_code = 429
                return web.Response(
                    text="Rate Limit Exceeded",
                    status=self.status_code,
                    headers={"Retry-After": self.retry_after_header}
                    if self.retry_after_header
                    else None,
                )
            return None

        def handle_clear_status(path: str) -> web.Response | None:
            # Simulate clear status for a specific path
            if path == "/clear_status":
                self.status_code = 200
                self.retry_after_header = None
                return web.Response(text="Default Response", status=self.status_code)
            return None

        def handle_add_response(
            path: str, query_string: dict[str, list[str]]
        ) -> web.Response | None:
            if path == "/add_response":
                patched_uri = query_string["uri"][0]
                if patched_uri in self.uri_mapping:
                    files = query_string.get("files")
                    if files is not None:
                        self.uri_mapping[patched_uri].extend(files)
                        return web.Response(
                            text="Default Response",
                            status=200,
                            headers={"Content-Type": "text/plain"},
                        )
            return web.Response(text="URI not found", status=404)

        if (retval := handle_rate_limit_exceeded(path)) is not None:
            return retval
        if (retval := handle_service_unavailable(path)) is not None:
            return retval
        if (retval := handle_clear_status(path)) is not None:
            return retval
        if (retval := handle_add_response(path, query_params)) is not None:
            return retval
        # do the actual request handling
        if (
            path == self._make_local_prefix(ADT_LOGIN_URI)
        ) and request.method == "POST":
            self.logged_in = True
            raise web.HTTPFound(ADT_SUMMARY_URI)
        if (
            path == self._make_local_prefix(ADT_LOGOUT_URI)
        ) and request.method == "POST":
            self.logged_in = False
            raise web.HTTPFound(ADT_LOGIN_URI)
        if not self.logged_in:
            return serve_file("signin_fail.html")
        if path == self._make_local_prefix(ADT_DEVICE_URI):
            device_id = query_params["id"][0]
            return serve_file(f"device-{device_id}.html")
        files_to_serve = self.uri_mapping.get(path)
        if not files_to_serve:
            return web.Response(text="URI not found", status=404)
        file_to_serve = files_to_serve[0]
        if len(files_to_serve) > 1:
            file_to_serve = self.uri_mapping[path].pop(1)
        return serve_file(file_to_serve)


@pytest.fixture
@pytest.mark.asyncio
async def mocked_pulse_server() -> PulseMockedWebServer:
    """Fixture to create a mocked Pulse server."""
    pulse_properties = get_mocked_connection_properties()
    m = PulseMockedWebServer(pulse_properties)
    return m
