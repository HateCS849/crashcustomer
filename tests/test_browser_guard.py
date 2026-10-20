from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from crashcustomer.browser import BrowserTools
from crashcustomer.models import Action


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:8710/demo/abc", True),
    ("http://127.0.0.1:8710/api/demo/abc/submit", True),
    ("http://127.0.0.1:8710/api/runs", False),
    ("http://127.0.0.1:8710/demo/another-session", False),
    ("https://example.com/", False),
    ("http://127.0.0.1:9999/demo/abc", False),
])
async def test_browser_can_reach_only_its_own_test_session(url, allowed):
    tools = BrowserTools("http://127.0.0.1:8710/demo/abc", Path("/tmp/unused"), "test", "explore")
    route = SimpleNamespace(request=SimpleNamespace(url=url), continue_=AsyncMock(), abort=AsyncMock())
    await tools._guard_request(route)
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)


async def test_unobserved_control_is_agent_error():
    tools = BrowserTools("http://127.0.0.1:8710/demo/abc", Path("/tmp/unused"), "test", "explore")
    with pytest.raises(ValueError, match="관찰된"):
        await tools.act(Action(kind="click", target="존재하지 않는 버튼", reason="test"), {"buttons": []})
