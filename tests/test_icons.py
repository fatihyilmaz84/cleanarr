"""The app icon is served and referenced correctly.

Cheap to break silently — a missing file or a renamed mount just shows the
browser's blank default, which nobody notices in review — so the wiring is
pinned here rather than left to a manual look at the tab.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import STATIC_DIR, create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "test.db")) as c:
        yield c


@pytest.mark.parametrize(
    "name, content_type",
    [
        ("favicon.ico", None),  # served as image/x-icon or image/vnd.microsoft.icon
        ("favicon-16.png", "image/png"),
        ("favicon-32.png", "image/png"),
        ("apple-touch-icon.png", "image/png"),
        ("icon-192.png", "image/png"),
        ("icon-512.png", "image/png"),
    ],
)
def test_icon_assets_are_served(client: TestClient, name: str, content_type: str | None):
    resp = client.get(f"/static/{name}")
    assert resp.status_code == 200, name
    assert resp.content, f"{name} is empty"
    if content_type:
        assert resp.headers["content-type"] == content_type


def test_bare_favicon_ico_is_served_at_the_root(client: TestClient):
    """Browsers request this on their own, before/regardless of the page's
    own <link rel="icon">.
    """
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.content[:4] == b"\x00\x00\x01\x00"  # ICO magic


def test_webmanifest_points_at_icons_that_exist(client: TestClient):
    manifest = client.get("/static/site.webmanifest")
    assert manifest.status_code == 200
    for icon in manifest.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_pages_reference_the_icon(client: TestClient):
    page = client.get("/").text
    assert '<link rel="icon" href="/static/favicon.ico"' in page
    assert 'rel="apple-touch-icon"' in page
    assert 'rel="manifest"' in page


def test_static_dir_resolves_independently_of_the_working_directory():
    """Mounted from the package's own location, not a relative path — uvicorn
    is started from / in some setups, and a relative mount would 404 there.
    """
    assert STATIC_DIR.is_absolute()
    assert (STATIC_DIR / "favicon.ico").is_file()
