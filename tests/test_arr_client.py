import httpx
import pytest

from app.arr_client import ArrClient, normalize_path


def test_normalize_path_handles_mixed_separators():
    assert normalize_path("/data/movies/Foo (2020)/Foo.mkv") == "/data/movies/Foo (2020)/Foo.mkv"
    assert normalize_path("/data/movies//Foo/./Foo.mkv") == "/data/movies/Foo/Foo.mkv"


RADARR_MOVIES = [
    {
        "id": 1,
        "title": "Example Movie",
        "year": 2020,
        "path": "/data/movies/Example Movie (2020)",
        "images": [{"coverType": "poster", "remoteUrl": "http://poster/1.jpg"}],
        "movieFile": {"relativePath": "Example Movie (2020).mkv"},
        "originalLanguage": {"id": 1, "name": "Korean"},
    },
    {
        "id": 2,
        "title": "No File Yet",
        "path": "/data/movies/No File Yet",
        "images": [],
        # no movieFile -> should be skipped
    },
]

SONARR_SERIES = [
    {
        "id": 10,
        "title": "Example Show",
        "images": [{"coverType": "poster", "url": "/poster/10.jpg"}],
        "originalLanguage": {"id": 2, "name": "Japanese"},
    },
]

SONARR_EPISODE_FILES = [
    {
        "id": 100,
        "seriesId": 10,
        "seasonNumber": 1,
        "path": "/data/tvshows/Example Show/Season 01/Example Show - S01E01.mkv",
        "sceneName": "Example.Show.S01E01",
    },
]


def make_client(handler) -> ArrClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return ArrClient(
        radarr_url="http://radarr:7878",
        radarr_api_key="radarr-key",
        sonarr_url="http://sonarr:8989",
        sonarr_api_key="sonarr-key",
        http_client=http_client,
    )


@pytest.mark.asyncio
async def test_build_movie_index_resolves_path_from_root_plus_relative():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "radarr-key"
        assert request.url.path == "/api/v3/movie"
        return httpx.Response(200, json=RADARR_MOVIES)

    client = ArrClient(radarr_url="http://radarr:7878", radarr_api_key="radarr-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    index, warnings = await client.build_movie_index()

    assert warnings == []
    assert len(index) == 1
    info = index["/data/movies/Example Movie (2020)/Example Movie (2020).mkv"]
    assert info.title == "Example Movie"
    assert info.year == 2020
    assert info.poster_url == "http://poster/1.jpg"
    assert info.kind == "movie"
    assert info.original_language == "Korean"


@pytest.mark.asyncio
async def test_build_series_index_fetches_per_series_episode_files():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            assert request.headers["X-Api-Key"] == "sonarr-key"
            return httpx.Response(200, json=SONARR_SERIES)
        if request.url.path == "/api/v3/episodefile":
            assert request.url.params["seriesId"] == "10"
            return httpx.Response(200, json=SONARR_EPISODE_FILES)
        return httpx.Response(404)

    client = ArrClient(sonarr_url="http://sonarr:8989", sonarr_api_key="sonarr-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    index, warnings = await client.build_series_index()

    assert warnings == []
    assert len(index) == 1
    info = index["/data/tvshows/Example Show/Season 01/Example Show - S01E01.mkv"]
    assert info.kind == "episode"
    assert info.series_title == "Example Show"
    assert info.season_number == 1
    assert info.original_language == "Japanese"


@pytest.mark.asyncio
async def test_build_index_merges_and_handles_partial_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if "radarr" in str(request.url):
            return httpx.Response(200, json=RADARR_MOVIES)
        if request.url.path == "/api/v3/series":
            return httpx.Response(500, text="internal error")
        return httpx.Response(404)

    client = make_client(handler)
    index, warnings = await client.build_index()

    assert len(index) == 1  # only the movie made it in
    assert any("Sonarr" in w for w in warnings)


@pytest.mark.asyncio
async def test_build_index_returns_empty_when_unconfigured():
    client = ArrClient()  # no urls/keys at all
    index, warnings = await client.build_index()
    assert index == {}
    assert warnings == []


# --- test_connection ---------------------------------------------------


def _status_client(handler) -> ArrClient:
    return ArrClient(
        radarr_url="http://radarr:7878",
        radarr_api_key="radarr-key",
        sonarr_url="http://sonarr:8989",
        sonarr_api_key="sonarr-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_test_connection_reports_the_version_on_success():
    def handler(request):
        assert request.url.path == "/api/v3/system/status"
        assert request.headers["X-Api-Key"] == "radarr-key"
        return httpx.Response(200, json={"version": "5.14.0.9383", "appName": "Radarr"})

    result = await _status_client(handler).test_connection("radarr")
    assert result.ok is True
    assert result.detail == "v5.14.0.9383"


@pytest.mark.asyncio
async def test_test_connection_calls_out_a_rejected_api_key_specifically():
    # By far the most likely misconfiguration — a bare "401" wouldn't tell
    # anyone which of the two fields to go and fix.
    result = await _status_client(lambda request: httpx.Response(401, json={})).test_connection("sonarr")
    assert result.ok is False
    assert "API key" in result.detail


@pytest.mark.asyncio
async def test_test_connection_reports_an_unreachable_service_without_raising():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    result = await _status_client(handler).test_connection("radarr")
    assert result.ok is False
    assert "Unreachable" in result.detail


@pytest.mark.asyncio
async def test_test_connection_reports_a_url_that_is_not_an_arr_at_all():
    result = await _status_client(lambda request: httpx.Response(200, text="<html>hello</html>")).test_connection(
        "radarr"
    )
    assert result.ok is False
    assert "URL" in result.detail


@pytest.mark.asyncio
async def test_test_connection_says_what_is_missing_when_unconfigured():
    result = await ArrClient(radarr_url="http://radarr:7878").test_connection("radarr")
    assert result.ok is False
    assert result.detail == "No API key configured"

    result = await ArrClient().test_connection("sonarr")
    assert result.ok is False
    assert result.detail == "No URL configured"
