"""Thin helpers over the generic `app_settings` key/value table for the
handful of concrete settings this app has: rule config, media paths, and
Sonarr/Radarr connection info.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import AppSetting
from app.rules import RuleConfig

RULES_KEY = "rules"
MEDIA_PATHS_KEY = "media_paths"
ARR_CONFIG_KEY = "arr_config"
DISPLAY_SETTINGS_KEY = "display_settings"
SCHEDULES_KEY = "schedules"


class ArrConfig(BaseModel):
    radarr_url: str | None = None
    radarr_api_key: str | None = None
    sonarr_url: str | None = None
    sonarr_api_key: str | None = None

    def redacted(self) -> "ArrConfig":
        return ArrConfig(
            radarr_url=self.radarr_url,
            radarr_api_key="***" if self.radarr_api_key else None,
            sonarr_url=self.sonarr_url,
            sonarr_api_key="***" if self.sonarr_api_key else None,
        )


async def _get(session: AsyncSession, key: str) -> dict | None:
    result = await session.exec(select(AppSetting).where(AppSetting.key == key))
    row = result.one_or_none()
    return row.value_json if row else None


async def _set(session: AsyncSession, key: str, value: dict) -> None:
    result = await session.exec(select(AppSetting).where(AppSetting.key == key))
    row = result.one_or_none()
    if row is None:
        row = AppSetting(key=key, value_json=value)
    else:
        row.value_json = value
    session.add(row)
    await session.commit()


async def get_rule_config(session: AsyncSession) -> RuleConfig:
    data = await _get(session, RULES_KEY)
    return RuleConfig.model_validate(data) if data else RuleConfig()


async def set_rule_config(session: AsyncSession, config: RuleConfig) -> None:
    await _set(session, RULES_KEY, config.model_dump())


class MediaPath(BaseModel):
    path: str
    library_type: str = "unknown"  # "movie" | "tv" | "unknown"


async def get_media_paths(session: AsyncSession) -> list[MediaPath]:
    data = await _get(session, MEDIA_PATHS_KEY)
    return [MediaPath.model_validate(p) for p in data.get("paths", [])] if data else []


async def set_media_paths(session: AsyncSession, paths: list[MediaPath]) -> None:
    await _set(session, MEDIA_PATHS_KEY, {"paths": [p.model_dump() for p in paths]})


async def get_arr_config(session: AsyncSession) -> ArrConfig:
    data = await _get(session, ARR_CONFIG_KEY)
    return ArrConfig.model_validate(data) if data else ArrConfig()


async def set_arr_config(session: AsyncSession, config: ArrConfig) -> None:
    await _set(session, ARR_CONFIG_KEY, config.model_dump())


class DisplaySettings(BaseModel):
    # IANA zone name (e.g. "Europe/Berlin"). Stored timestamps are always
    # UTC; this only controls what timezone they're rendered in.
    timezone: str = "UTC"


async def get_display_settings(session: AsyncSession) -> DisplaySettings:
    data = await _get(session, DISPLAY_SETTINGS_KEY)
    return DisplaySettings.model_validate(data) if data else DisplaySettings()


async def set_display_settings(session: AsyncSession, settings: DisplaySettings) -> None:
    await _set(session, DISPLAY_SETTINGS_KEY, settings.model_dump())


class Schedule(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    label: str = ""
    enabled: bool = True
    hour: int = 4
    minute: int = 0
    # Python's date.weekday(): 0=Monday .. 6=Sunday. Defaults to every day.
    days_of_week: list[int] = Field(default_factory=lambda: list(range(7)))
    # Scanning only ever proposes changes for review — this is the one
    # explicit opt-in to actually apply them unattended. Off by default.
    auto_apply: bool = False


async def get_schedules(session: AsyncSession) -> list[Schedule]:
    data = await _get(session, SCHEDULES_KEY)
    return [Schedule.model_validate(s) for s in data.get("schedules", [])] if data else []


async def set_schedules(session: AsyncSession, schedules: list[Schedule]) -> None:
    await _set(session, SCHEDULES_KEY, {"schedules": [s.model_dump() for s in schedules]})
