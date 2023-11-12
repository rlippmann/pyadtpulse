"""conftest.py"""
import asyncio
import os
import sys

import aiohttp
import pytest
from bs4 import BeautifulSoup
from bs4.element import Comment
from yarl import URL

# Get the absolute path of the directory containing the conftest.py file
current_dir = os.path.dirname(os.path.abspath(__file__))

# Add the parent directory (source tree) to sys.path
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# pylint: disable=wrong-import-position
from pyadtpulse.const import DEFAULT_API_HOST  # noqa: E402
from pyadtpulse.pulse_query_manager import PulseQueryManager  # noqa: E402


@pytest.fixture
def remove_comments_and_javascript():
    def _remove_comments_and_javascript(html):
        soup = BeautifulSoup(html, "html.parser")

        # Remove HTML comments
        comments = soup.find_all(text=lambda text: isinstance(text, Comment))
        for comment in comments:
            comment.extract()

        # Remove <script> tags and their contents
        script_tags = soup.find_all("script")
        for script_tag in script_tags:
            script_tag.extract()

        return soup

    return _remove_comments_and_javascript


@pytest.fixture
def compare_html(remove_comments_and_javascript):
    def _compare_html(html1, html2):
        cleaned_html1 = remove_comments_and_javascript(html1)
        cleaned_html2 = remove_comments_and_javascript(html2)

        return cleaned_html1 == cleaned_html2

    return _compare_html


def make_sync_check(a: int, b: int) -> str:
    """Make a sync check string."""
    return "-".join([str(a), str(b), "0"])


@pytest.fixture
def read_test_file(filename: str) -> str:
    """Read a test file."""
    subdirectory = os.path.join(current_dir, "html")
    path = os.path.join(subdirectory, filename)
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
@pytest.mark.asyncio
async def signin_page() -> tuple[int, str, URL | None]:
    """Generate a live signin page."""
    response: aiohttp.ClientResponse | None = None
    url: URL | None = None
    async with aiohttp.ClientSession() as session:
        try:
            response = await session.request("GET", DEFAULT_API_HOST)
            response.raise_for_status()
            text = await response.text()
            code = response.status
        except (
            aiohttp.ClientError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientResponseError,
            asyncio.TimeoutError,
        ) as e:
            code = response.status if response else getattr(e, "code")
            text = str(e)
            url = response.url if response else None
            pytest.skip("signin_page returned exception")

    return (code, text, url)


@pytest.mark.asyncio
@pytest.fixture
async def get_api_version() -> str | None:
    """Fetch the API version from the signin page."""
    _, _, url = await signin_page()
    return PulseQueryManager._get_api_version(  # pylint: disable=protected-access
        str(url)
    )
