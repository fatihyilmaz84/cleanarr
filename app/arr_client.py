"""Read-only Sonarr/Radarr connector.

Used only to resolve a file path to a human title/poster for display in the
UI. Never writes anything back to Sonarr/Radarr and never triggers a
re-import — Cleanarr rewrites files in place via atomic replace, so from
Sonarr/Radarr's point of view the file's path, name, and (for practical
purposes) size never change enough to look "missing" or "upgraded".
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    original_language: str | None = None  # e.g. "Korean" — Radarr/Sonarr's originalLanguage.name


@dataclass(frozen=True)
class ArrConnectionResult:
    service: str  # "radarr" | "sonarr"
    ok: bool
    detail: str  # version string when ok, else why not — shown verbatim in Settings


def normalize_path(path: str) -> str:
    """Normalize to a posix-style path for use as a lookup key. The whole
    stack (Sonarr/Radarr/Jellyfin/Cleanarr) runs in Linux containers sharing
    the same bind mount, so paths are always posix-style at runtime.
    """
    return posixpath.normpath(path.replace("\\", "/"))


def display_title_for(info: "ArrMediaInfo") -> str:
    """How a file should be labelled in the UI.

    Episodes are "Series - S01E01 - Kassa". The parts are joined only when
    they're actually distinct: with nothing to say about the episode this
    used to render f"{series} - {title}" where title had already fallen back
    to the series name, giving "Andor - Andor" on 35% of one real library's
    episodes.
    """
    if info.kind == "movie":
        return info.title
    parts = [info.series_title or "", info.title or ""]
    kept = [p for p in parts if p]
    if len(kept) == 2 and kept[0] == kept[1]:
        kept = kept[:1]
    return " - ".join(kept)


def _episode_label(episodes: list[dict]) -> str | None:
    """"S01E01 - Kassa" for one episode, "S01E01-E02 - Title" for a file
    covering several. None when Sonarr can't say which episode it is.
    """
    numbered = sorted(
        (e for e in episodes if e.get("seasonNumber") is not None and e.get("episodeNumber") is not None),
        key=lambda e: (e["seasonNumber"], e["episodeNumber"]),
    )
    if not numbered:
        return None

    season = numbered[0]["seasonNumber"]
    code = f"S{season:02d}E{numbered[0]['episodeNumber']:02d}"
    if len(numbered) > 1:
        code += f"-E{numbered[-1]['episodeNumber']:02d}"

    title = numbered[0].get("title")
    return f"{code} - {title}" if title else code


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

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """One client for a whole index build, rather than one per request.

        build_series_index issues a request per series — on a library with
        hundreds of shows that used to mean hundreds of fresh AsyncClients,
        each paying its own connection setup and throwing the connection
        away afterwards. One client keeps the pool alive across them.
        """
        if self._injected_client is not None:
            yield self._injected_client
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                yield client

    async def _get(
        self,
        base_url: str,
        api_key: str,
        path: str,
        params: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> object:
        headers = {"X-Api-Key": api_key}
        url = f"{base_url}{path}"
        if client is not None:
            resp = await client.get(url, headers=headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        async with self._session() as owned:
            resp = await owned.get(url, headers=headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    async def build_movie_index(
        self, client: httpx.AsyncClient | None = None
    ) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        if not (self.radarr_url and self.radarr_api_key):
            return {}, []

        warnings: list[str] = []
        index: dict[str, ArrMediaInfo] = {}
        try:
            movies = await self._get(self.radarr_url, self.radarr_api_key, "/api/v3/movie", client=client)
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
                original_language=(movie.get("originalLanguage") or {}).get("name"),
            )
        return index, warnings

    async def build_series_index(
        self, client: httpx.AsyncClient | None = None
    ) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        if not (self.sonarr_url and self.sonarr_api_key):
            return {}, []

        try:
            series_list = await self._get(self.sonarr_url, self.sonarr_api_key, "/api/v3/series", client=client)
        except (httpx.HTTPError, ValueError) as e:
            return {}, [f"Sonarr: failed to fetch series list ({e})"]

        # Two requests per series: the files, and the episodes that name
        # them. Sequentially that's a scan-blocking round trip per show, so
        # they go concurrently — capped, so a library with hundreds of shows
        # doesn't fire hundreds of requests at Sonarr at once.
        semaphore = asyncio.Semaphore(8)

        async def fetch_series_data(series: dict) -> tuple[dict, list[dict] | None, list[dict], str | None]:
            series_title = series.get("title", "Unknown")
            async with semaphore:
                try:
                    episode_files = await self._get(
                        self.sonarr_url,
                        self.sonarr_api_key,
                        "/api/v3/episodefile",
                        params={"seriesId": series.get("id")},
                        client=client,
                    )
                except (httpx.HTTPError, ValueError) as e:
                    return series, None, [], f"Sonarr: failed to fetch episode files for '{series_title}' ({e})"
                try:
                    episodes = await self._get(
                        self.sonarr_url,
                        self.sonarr_api_key,
                        "/api/v3/episode",
                        params={"seriesId": series.get("id")},
                        client=client,
                    )
                except (httpx.HTTPError, ValueError):
                    # Only costs the episode names; the files themselves are
                    # already in hand, so enrichment degrades rather than fails.
                    episodes = []
            return series, episode_files, episodes, None

        results = await asyncio.gather(*(fetch_series_data(s) for s in series_list))

        warnings: list[str] = []
        index: dict[str, ArrMediaInfo] = {}
        for series, episode_files, episodes, warning in results:
            if warning:
                warnings.append(warning)
                continue

            episodes_by_file: dict[int, list[dict]] = {}
            for episode in episodes:
                file_id = episode.get("episodeFileId")
                if file_id:
                    episodes_by_file.setdefault(file_id, []).append(episode)

            series_id = series.get("id")
            series_title = series.get("title", "Unknown")
            poster_url = _poster_url(series.get("images"))
            original_language = (series.get("originalLanguage") or {}).get("name")

            for ep_file in episode_files:
                file_path = ep_file.get("path")
                if not file_path:
                    continue
                own_episodes = episodes_by_file.get(ep_file.get("id"), [])
                label = _episode_label(own_episodes)
                numbers = [e.get("episodeNumber") for e in own_episodes if e.get("episodeNumber") is not None]
                index[normalize_path(file_path)] = ArrMediaInfo(
                    kind="episode",
                    # The episode's own number and name where Sonarr knows
                    # them. sceneName is the raw release string
                    # ("Show.S01E02.1080p.WEB-DL.x264-GRP") and is often
                    # absent entirely, so it's only a fallback.
                    title=label or ep_file.get("sceneName") or "",
                    series_title=series_title,
                    season_number=ep_file.get("seasonNumber"),
                    episode_number=min(numbers) if numbers else None,
                    poster_url=poster_url,
                    arr_id=series_id,
                    original_language=original_language,
                )
        return index, warnings

    async def test_connection(self, service: str) -> "ArrConnectionResult":
        """Ask one service whether it's reachable and the API key works.

        Uses /api/v3/system/status — the cheapest authenticated endpoint
        both Sonarr and Radarr expose, so this stays a fast round trip
        rather than pulling a whole library down just to prove reachability.
        Never raises: an unreachable service is an answer, not an error.
        """
        if service == "radarr":
            base_url, api_key = self.radarr_url, self.radarr_api_key
        elif service == "sonarr":
            base_url, api_key = self.sonarr_url, self.sonarr_api_key
        else:
            raise ValueError(f"unknown service '{service}'")

        if not base_url or not api_key:
            missing = "URL" if not base_url else "API key"
            return ArrConnectionResult(service, False, f"No {missing} configured")

        try:
            status = await self._get(base_url, api_key, "/api/v3/system/status")
        except httpx.HTTPStatusError as e:
            # A wrong key is the single most likely misconfiguration, and
            # "401" on its own doesn't tell anyone what to go fix.
            if e.response.status_code in (401, 403):
                return ArrConnectionResult(service, False, "Rejected the API key (401/403)")
            return ArrConnectionResult(service, False, f"HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            return ArrConnectionResult(service, False, f"Unreachable ({type(e).__name__})")
        except ValueError:
            return ArrConnectionResult(service, False, "Responded, but not with valid JSON — is that really the URL?")

        if not isinstance(status, dict):
            return ArrConnectionResult(service, False, "Unexpected response shape")
        version = status.get("version")
        return ArrConnectionResult(service, True, f"v{version}" if version else "Connected")

    async def build_index(self) -> tuple[dict[str, ArrMediaInfo], list[str]]:
        """Merged path -> ArrMediaInfo lookup across Radarr + Sonarr, plus any
        non-fatal warnings (e.g. one service unreachable) collected along the
        way. Never raises for a single unreachable service — a scan should
        still proceed with degraded enrichment rather than fail entirely.
        """
        async with self._session() as client:
            movie_index, movie_warnings = await self.build_movie_index(client=client)
            series_index, series_warnings = await self.build_series_index(client=client)
        return {**movie_index, **series_index}, [*movie_warnings, *series_warnings]
