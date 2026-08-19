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
from app.normalizer import NormalizerConfig
from app.rules import RuleConfig

RULES_KEY = "rules"
MEDIA_PATHS_KEY = "media_paths"
ARR_CONFIG_KEY = "arr_config"
DISPLAY_SETTINGS_KEY = "display_settings"
SCHEDULES_KEY = "schedules"
NORMALIZER_CONFIG_KEY = "normalizer_config"
RULE_PRESETS_KEY = "rule_presets"
NORMALIZER_PRESETS_KEY = "normalizer_presets"


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


class RulePreset(BaseModel):
    """A named, saved RuleConfig that a Schedule can point at (see
    Schedule.rule_preset_id) — e.g. a gentle "Nightly" set and an
    aggressive "Weekly deep clean" set, each attached to its own schedule.

    Purely additive: the Rules page's own config stays the unnamed
    "Default", used by manual Scan Now and by any schedule with no preset
    attached. Nothing referencing a preset is ever hard-broken by deleting
    it — see resolve_rule_config, which falls back to Default.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    config: RuleConfig = Field(default_factory=RuleConfig)


class NormalizerPreset(BaseModel):
    """NormalizerConfig equivalent of RulePreset — the normalizer is an
    independent system from the drop engine (see app/normalizer.py), so it
    gets its own separate preset list rather than sharing one.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    config: NormalizerConfig = Field(default_factory=NormalizerConfig)


async def get_rule_presets(session: AsyncSession) -> list[RulePreset]:
    data = await _get(session, RULE_PRESETS_KEY)
    return [RulePreset.model_validate(p) for p in data.get("presets", [])] if data else []


async def set_rule_presets(session: AsyncSession, presets: list[RulePreset]) -> None:
    await _set(session, RULE_PRESETS_KEY, {"presets": [p.model_dump() for p in presets]})


async def get_normalizer_presets(session: AsyncSession) -> list[NormalizerPreset]:
    data = await _get(session, NORMALIZER_PRESETS_KEY)
    return [NormalizerPreset.model_validate(p) for p in data.get("presets", [])] if data else []


async def set_normalizer_presets(session: AsyncSession, presets: list[NormalizerPreset]) -> None:
    await _set(session, NORMALIZER_PRESETS_KEY, {"presets": [p.model_dump() for p in presets]})


async def resolve_rule_config(session: AsyncSession, preset_id: str | None) -> RuleConfig:
    """The RuleConfig a run/apply identified by `preset_id` should use.

    `None` means the Default (the Rules page's own config). An id that no
    longer resolves — the preset was deleted after a schedule or a proposed
    change was stamped with it — also falls back to Default rather than
    raising: a stale reference must never wedge a scheduled run or block
    an already-queued change from being applied.
    """
    if preset_id:
        for preset in await get_rule_presets(session):
            if preset.id == preset_id:
                return preset.config
    return await get_rule_config(session)


async def resolve_normalizer_config(session: AsyncSession, preset_id: str | None) -> NormalizerConfig:
    """NormalizerConfig equivalent of resolve_rule_config, same fallback."""
    if preset_id:
        for preset in await get_normalizer_presets(session):
            if preset.id == preset_id:
                return preset.config
    return await get_normalizer_config(session)


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

    # --- Cleaning (the rule-based track remover, app/rules.py) ---
    # On by default: a schedule created before this field existed, and the
    # form's own default, both mean "this is a cleaning schedule".
    run_clean: bool = True
    # Which saved RulePreset the cleaning scan proposes with. None = the
    # Rules page's own Default config. The id is also stamped onto every
    # PendingChange this run produces (see app/scanner.py), so applying it
    # later re-decides with the *same* rules that proposed it, no matter
    # what triggers the apply.
    rule_preset_id: str | None = None
    # Scanning only ever proposes changes for review — this is the one
    # explicit opt-in to apply what *this run's own scan* finds, unattended
    # and unreviewed. Off by default.
    auto_apply: bool = False
    # Separate opt-in: also apply anything already sitting in the Queue
    # (status=approved) from a prior manual review. Lower-risk than
    # auto_apply — a human already confirmed these specific changes, this
    # just runs them on a schedule instead of a manual "Run Queue" click.
    apply_queued: bool = False

    # --- Normalizing (the track metadata normalizer, app/normalizer.py) ---
    # Off by default so existing schedules keep doing exactly what they did
    # before this existed. The normalizer is an independent system, so it
    # gets its own preset and its own pair of apply opt-ins rather than
    # riding on the cleaning ones.
    run_normalize: bool = False
    normalizer_preset_id: str | None = None
    normalize_auto_apply: bool = False
    normalize_apply_queued: bool = False
    # Optional end of the run window (e.g. hour=4/end_hour=6 -> "04:00-06:00").
    # Both None means "no limit, run to completion" (the old, still-default
    # behavior). A run that hits the deadline stops *between* files — it
    # never aborts a file mid-remux — so anything left over just stays
    # queued for the next scheduled run or a manual "Run Queue".
    # end_hour <= hour is treated as the window spanning past midnight
    # (e.g. 23:00-02:00), not as a same-day, negative-length window.
    end_hour: int | None = None
    end_minute: int | None = None


async def get_schedules(session: AsyncSession) -> list[Schedule]:
    data = await _get(session, SCHEDULES_KEY)
    return [Schedule.model_validate(s) for s in data.get("schedules", [])] if data else []


async def set_schedules(session: AsyncSession, schedules: list[Schedule]) -> None:
    await _set(session, SCHEDULES_KEY, {"schedules": [s.model_dump() for s in schedules]})


async def get_normalizer_config(session: AsyncSession) -> NormalizerConfig:
    data = await _get(session, NORMALIZER_CONFIG_KEY)
    return NormalizerConfig.model_validate(data) if data else NormalizerConfig()


async def set_normalizer_config(session: AsyncSession, config: NormalizerConfig) -> None:
    await _set(session, NORMALIZER_CONFIG_KEY, config.model_dump())
