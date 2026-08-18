"""Timestamps are always stored in UTC (see app/models.py's `utcnow`) — these
cover the display-side conversion: the `localtime` Jinja filter itself, and
the Settings page round-trip that lets a user pick which timezone to render
in (see TODO.md #4 — history/overview previously rendered raw UTC, which
looked "wrong" against the server's actual local time).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.web import _localtime


def test_localtime_converts_utc_to_target_zone():
    dt = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)  # 04:00 UTC
    converted = _localtime(dt, "Europe/Berlin")  # UTC+1 in January
    assert converted.hour == 5


def test_localtime_falls_back_to_utc_for_unknown_zone():
    dt = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    converted = _localtime(dt, "Not/AZone")
    assert converted.hour == 4  # fell back to UTC unchanged, didn't raise


def test_localtime_passes_through_none():
    assert _localtime(None, "UTC") is None


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app, follow_redirects=True) as c:
        yield c


def test_save_display_timezone_via_settings_form(client: TestClient):
    resp = client.post("/settings/display", data={"timezone": "Europe/Berlin"})
    assert resp.status_code == 200
    assert "Display timezone saved" in resp.text

    settings_page = client.get("/settings").text
    assert '<option value="Europe/Berlin" selected>' in settings_page


def test_save_display_timezone_rejects_unknown_zone(client: TestClient):
    resp = client.post("/settings/display", data={"timezone": "Not/AZone"})
    assert "not saved" in resp.text.lower()

    settings_page = client.get("/settings").text
    assert '<option value="UTC" selected>' in settings_page  # unchanged, still default
