"""Test Pulse Query Manager."""
from typing import Callable
import aiohttp


import pytest


@pytest.fixture
@pytest.mark.asyncio
async def test_mocked_responses(
    read_file: Callable[[str], str],
    mocked_server_responses,
    get_mocked_mapped_static_responses: dict[str, str],
):
    """Fixture to test mocked responses."""
    static_responses = get_mocked_mapped_static_responses
    with mocked_server_responses:
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
