"""Read-only Sonarr/Radarr connector.

Used only to resolve a file path to a human title/poster for display in the
UI. Never writes anything back to Sonarr/Radarr and never triggers a
re-import — Cleanarr rewrites files in place via atomic replace, so from
Sonarr/Radarr's point of view the file's path, name, and (for practical
purposes) size never change enough to look "missing" or "upgraded".
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

import httpx

DEFAULT_TIMEOUT_SECONDS = 15.0


class ArrClientError(RuntimeError):
    """Raised for a hard failure talking to Sonarr/Radarr. Callers should
    treat this as "enrichment unavailable", not fatal to a scan.
    """


@dataclass(frozen=True)
class ArrMediaInfo:
    kind: str  # "movie" | "episode"
    title: str
    poster_url: str | None = None
    year: int | None = None
    series_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    arr_id: int | None = None


def normalize_path(path: str) -> str:
    """Normalize to a posix-style path for use as a lookup key. The whole
    stack (Sonarr/Radarr/Jellyfin/Cleanarr) runs in Linux containers sharing
    the same bind mount, so paths are always posix-style at runtime.
    """
    return posixpath.normpath(path.replace("\\", "/"))


def _poster_url(images: list[dict] | None) -> str | None:
    for image in images or []:
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url")
    return None


class ArrClient:
    def __init__(
        self,
        radarr_url: str | None = None,
        radarr_api_key: str | None = None,
        sonarr_url: str | None = None,
        sonarr_api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.radarr_url = radarr_url.rstrip("/") if radarr_url else None
        self.radarr_api_key = radarr_api_key
        self.sonarr_url = sonarr_url.rstrip("/") if sonarr_url else None
        self.sonarr_api_key = sonarr_api_key
        self._injected_client = http_client
        self.timeout = timeout

    async def _get(self, base_url: str, api_key: str, path: str, params: dict | None = None) -> object:
        headers = {"X-Api-Key": api_key}
        url = f"{base_url}{path}"
        if self._injected_client is not None:
            resp = await self._injected_client.get(url, headers=headers, params=params, timeout=self.timeout)
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    async def build_movie_index(self) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        if not (self.radarr_url and self.radarr_api_key):
            return {}, []

        warnings: list[str] = []
        index: dict[str, ArrMediaInfo] = {}
        try:
            movies = await self._get(self.radarr_url, self.radarr_api_key, "/api/v3/movie")
        except (httpx.HTTPError, ValueError) as e:
            return {}, [f"Radarr: failed to fetch movie list ({e})"]

        for movie in movies:
            movie_file = movie.get("movieFile")
            if not movie_file:
                continue
            file_path = movie_file.get("path")
            if not file_path:
                root = movie.get("path")
                relative = movie_file.get("relativePath")
                if not (root and relative):
                    warnings.append(f"Radarr: movie '{movie.get('title')}' has a file but no resolvable path")
                    continue
                file_path = f"{root.rstrip('/')}/{relative}"

            index[normalize_path(file_path)] = ArrMediaInfo(
                kind="movie",
                title=movie.get("title", "Unknown"),
                year=movie.get("year"),
                poster_url=_poster_url(movie.get("images")),
                arr_id=movie.get("id"),
            )
        return index, warnings

    async def build_series_index(self) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        if not (self.sonarr_url and self.sonarr_api_key):
            return {}, []

        warnings: list[str] = []
        index: dict[str, ArrMediaInfo] = {}
        try:
            series_list = await self._get(self.sonarr_url, self.sonarr_api_key, "/api/v3/series")
        except (httpx.HTTPError, ValueError) as e:
            return {}, [f"Sonarr: failed to fetch series list ({e})"]

        for series in series_list:
            series_id = series.get("id")
            series_title = series.get("title", "Unknown")
            poster_url = _poster_url(series.get("images"))
            try:
                episode_files = await self._get(
                    self.sonarr_url, self.sonarr_api_key, "/api/v3/episodefile", params={"seriesId": series_id}
                )
            except (httpx.HTTPError, ValueError) as e:
                warnings.append(f"Sonarr: failed to fetch episode files for '{series_title}' ({e})")
                continue

            for ep_file in episode_files:
                file_path = ep_file.get("path")
                if not file_path:
                    continue
                index[normalize_path(file_path)] = ArrMediaInfo(
                    kind="episode",
                    title=ep_file.get("sceneName") or series_title,
                    series_title=series_title,
                    season_number=ep_file.get("seasonNumber"),
                    poster_url=poster_url,
                    arr_id=series_id,
                )
        return index, warnings

    async def build_index(self) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        """Merged path -> ArrMediaInfo lookup across Radarr + Sonarr, plus any
        non-fatal warnings (e.g. one service unreachable) collected along the
        way. Never raises for a single unreachable service — a scan should
        still proceed with degraded enrichment rather than fail entirely.
        """
        movie_index, movie_warnings = await self.build_movie_index()
        series_index, series_warnings = await self.build_series_index()
        return {**movie_index, **series_index}, [*movie_warnings, *series_warnings]
