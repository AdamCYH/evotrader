"""The console's files are checked for a newer version on every load.

After an update, a browser that had the console open before ran the new
``js/app.js`` with its kept copy of ``js/api.js``: the page names its modules
by fixed paths, and with no Cache-Control header a browser reuses a file for
hours by its own estimate. The old ``api.js`` dropped the chosen instrument
from the chart request, so the market panel's picker went back to the primary
every time it was changed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "evotrader" / "web" / "static"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from evotrader.config import AppConfig
    from evotrader.web.server import create_app

    monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")
    app = create_app(
        db=MagicMock(),
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=MagicMock(),
        config=AppConfig(data_dir=tmp_path / "data"),
        runner_fn=AsyncMock(),
        memory=MagicMock(),
    )
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/index.html", "/js/app.js", "/js/api.js", "/style.css"])
def test_every_console_file_is_checked_on_every_load(client, path) -> None:
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_an_unchanged_file_costs_a_304(client) -> None:
    first = client.get("/js/api.js")
    again = client.get("/js/api.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def _import_map() -> dict[str, str]:
    html = (_STATIC / "index.html").read_text()
    found = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert found, "no import map in index.html"
    assert html.index('type="importmap"') < html.index('type="module"'), (
        "an import map after the first module script is ignored"
    )
    return json.loads(found.group(1))["imports"]


def test_the_import_map_names_files_that_exist(client) -> None:
    """A browser that kept the old js/api.js loads it under a new address."""
    imports = _import_map()
    assert imports["./js/api.js"].startswith("./js/api.js?")
    for key, value in imports.items():
        for url in (key, value):
            assert (_STATIC / url.split("?")[0].removeprefix("./")).is_file(), url
        assert client.get(value.removeprefix(".")).status_code == 200
