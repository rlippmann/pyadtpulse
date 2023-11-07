import logging

from pyadtpulse.util import remove_prefix

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)


class TestRemovePrefix:
    # prefix is at the beginning of the text
    def test_prefix_at_beginning(self):
        assert remove_prefix("hello world", "hello") == " world"

    # prefix is not in the text
    def test_prefix_not_in_text(self):
        assert remove_prefix("hello world", "hi") == "hello world"

    # prefix is an empty string
    def test_empty_prefix(self):
        assert remove_prefix("hello world", "") == "hello world"

    # prefix is the entire text
    def test_entire_text_as_prefix(self):
        assert remove_prefix("hello world", "hello world") == ""

    # prefix is longer than the text
    def test_longer_prefix(self):
        assert remove_prefix("hello", "hello world") == "hello"

    # text is an empty string
    def test_empty_text(self):
        assert remove_prefix("", "hello") == ""
