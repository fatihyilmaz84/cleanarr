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


def test_the_sidebar_shows_the_app_logo(client):
    """The top-left corner had a generic Material Symbols glyph
    (movie_filter) rather than the app's own icon, which every other surface
    — tab, home screen, PWA manifest — already used.
    """
    page = client.get("/").text
    assert '<img src="/static/icon-192.png"' in page
    assert "movie_filter" not in page
    # and it's served
    assert client.get("/static/icon-192.png").status_code == 200


def test_the_image_carries_the_labels_unraid_reads():
    """Unraid's Docker page uses these to give the container a WebUI entry in
    its context menu and its own icon instead of an anonymous square. Baked
    into the image so a plain `docker run` gets them too.
    """
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    content = dockerfile.read_text()
    assert 'net.unraid.docker.managed="dockerman"' in content
    assert 'net.unraid.docker.webui="http://[IP]:[PORT:8420]/"' in content
    assert "net.unraid.docker.icon=" in content
    # The port in the webui label has to match the one the image exposes,
    # or the menu entry opens nothing.
    assert "EXPOSE 8420" in content
