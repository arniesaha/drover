"""Tests for drover.server.web.ui page loading."""

import pytest

from drover.server.web.ui import load_page


@pytest.mark.parametrize(
    "name, marker",
    [
        ("observatory.html", "<!doctype html>"),
        ("harness.html", "<!doctype html>"),
        ("harness_terminal.html", "xterm"),
        ("login.html", "<form"),
    ],
)
def test_load_page_returns_content(name, marker):
    content = load_page(name)
    assert marker.lower() in content.lower()
    # Cached: same object back on second call.
    assert load_page(name) is content


def test_load_page_unknown_name():
    with pytest.raises(FileNotFoundError):
        load_page("nope.html")
