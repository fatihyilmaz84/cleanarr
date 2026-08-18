# Cleanarr TODO

Feature backlog from the 2026-08-18 session. Working through these one at a
time, not all at once — check items off as they land, in whatever order
makes sense (see "Suggested order" at the bottom for dependencies).

## 1. Per-track selective apply — done ✅

Right now, approving a pending change applies *every* stream the rule engine
proposed dropping for that file — no way to say "drop the audio track but
keep the subtitle" for one specific file.

- [x] Add an `overrides` column to `PendingChange` (list of stream indices
      the user force-kept despite the rule engine flagging them) — needs a
      migration via `app/db.py`'s `_add_missing_columns`, same pattern as
      `original_language`.
- [x] Review Queue: render each proposed-drop stream with a checkbox
      ("drop this track", checked by default = today's behavior), inside
      the approve form.
- [x] `POST /review/{id}/approve`: read which drop-checkboxes stayed
      checked, store the unchecked ones as `overrides`.
- [x] `apply_pending_change` (`app/apply.py`): after re-deciding against the
      *current* rules (still don't trust the stale scan-time decision —
      the file/rules may have changed since queueing), force-flip any
      stream index in `overrides` back to `keep=True` before handing off
      to `apply_remux`.
- [ ] Same override support for the bulk-approve path
      (`/api/review/approve-bulk`, and whatever UI Task 2 adds) — not done
      yet, out of scope for this pass.

## 2. Job queue (queue-then-run, not apply-on-approve) — done ✅

Today, "Approve & Apply" immediately submits an apply job. Wanted instead:
select multiple review items, queue them up, then hit one "Run Queue"
action that processes everything at once.

- [x] Repurposed `ChangeStatus.approved` as the "queued, not yet run" state
      — `app/web.py`'s `ui_approve` (Review Queue's "Add to Queue" button)
      now only sets status + overrides, no longer calls `submit_apply_job`.
      The JSON API's `/api/review/{id}/approve` and `/approve-bulk` are
      unchanged (still apply immediately) — kept as the programmatic
      "apply now" contract, distinct from the UI's queue-then-run flow.
- [x] New `/queue` page: lists everything in `approved` status (via
      `list_review_items(session, ChangeStatus.approved)`), with a
      "Run Queue (N)" button that calls `submit_apply_job` with every
      queued id at once, and a per-item "Remove from Queue" button
      (reverts to `pending`, clears overrides).
- [x] `queries.py::review_item` now applies overrides when computing
      kept/dropped, so the Queue page shows the *actual* plan (post
      per-track override) rather than the raw rule proposal — a no-op for
      still-pending items, since overrides is always empty there.
- [x] Overview page gained a "Queued" stat card + nav badge, mirroring the
      existing "Pending Review" one.
- [ ] Cross-item bulk-select on the Review Queue itself (checkbox per card
      + "Queue Selected") — not done; today you queue one item at a time
      from Review, which already covers "add tasks then tap run to run
      them". Revisit only if that turns out to be too slow in practice.

## 3. Scheduler (e.g. "every night at 04:00, scan + apply") — done ✅

- [x] Depends on Task 4 (timezone fix) — "04:00" has to mean 04:00 in a
      well-defined timezone, or it'll be wrong from day one.
- [x] New settings model (`app/settings_store.py::Schedule`): time of day,
      days of week, enabled flag, `auto_apply` flag. Stored via the
      existing `app_settings` key/value pattern.
- [x] Lightweight in-process scheduler (`app/scheduler.py`): an asyncio task
      (started/stopped alongside `JobManager` in `app/main.py`'s lifespan)
      that polls every 30s, checks configured schedules against the current
      time *in the configured display timezone*, and calls `submit_scan_job`
      on a match. No new dependency — consistent with the existing
      "deliberately minimal" job runner in `app/jobs.py`.
- [x] Guards against double-firing within the same minute (tracks
      last-fired-minute per schedule id in memory).
- [x] New `/schedule` page to manage schedules (add/enable/disable/delete,
      shows next-run time).
- [x] Resolved: `auto_apply` is an explicit, off-by-default opt-in per
      schedule (labeled "no review" in the UI) — a schedule with it off
      only scans and leaves everything in the Review Queue as normal.
      `submit_scan_job` gained an `auto_apply` param that, only when set,
      applies every resulting pending change unattended (no per-track
      overrides — there's no one there to check a box).

## 4. Fix timezone handling — done ✅

Confirmed root cause: the Docker container has no `TZ` set (defaults to
UTC), and every timestamp is stored via `datetime.now(timezone.utc)` in
`app/models.py`. That's correct and unambiguous internally, but
`overview.html`/`history.html` rendered it raw via `.strftime(...)` with no
conversion, so it looked "wrong" against the user's actual local time.

- [x] Resolved as a **Settings-based display timezone** rather than a
      container `TZ` env var — instantly changeable without a redeploy, and
      works the same regardless of what timezone the Docker host itself is
      in. Added `tzdata` to requirements.txt since the slim base image has
      no system tz database for Python's `zoneinfo` to fall back on.
- [x] `localtime` Jinja filter (`app/web.py`) converts a stored UTC
      datetime to the configured zone at render time; storage stays UTC
      throughout.
- [x] New "Display" section on the Settings page — IANA timezone dropdown,
      defaults to UTC.

## 5. Normalize track names across containers — mostly done ✅

Different muxers/tools tag the same kind of track differently (e.g. a
commentary track's `title` might be "Commentary", "Director's Commentary",
"commentary track", or nothing at all; hearing-impaired subs might be
"SDH", "English (SDH)", "Hearing Impaired", or rely solely on the
`disposition` flag).

- [x] Title-text fallback for `is_commentary`/`is_hearing_impaired`
      classification — implemented in `app/rules.py` (`_commentary_reason`/
      `_hearing_impaired_reason`), not `app/analyzer.py` as originally
      sketched, since the patterns needed to be *user-configurable*
      (RuleConfig, not a hardcoded constant) — analyzer.py has no access to
      rule config, it's a pure ffprobe-normalization layer. Checks the
      disposition flag first, falls back to regex title-matching against
      `RuleConfig.commentary_title_patterns` /
      `hearing_impaired_title_patterns`. Ships with sensible non-empty
      defaults (`["commentary"]` / `["sdh", "hearing.impaired"]`) — unlike
      the language keep-lists, "commentary"/"SDH" are unambiguous signals,
      no reason to make a user type them from scratch — fully editable in
      Rules.
- [x] New `drop_hearing_impaired_tracks` rule (mirrors the existing
      `drop_commentary_tracks`) — previously `is_hearing_impaired` was
      captured on every stream but nothing ever acted on it.
- [ ] Normalize *display* of track titles in the Review Queue (currently
      raw `s.title` shown as-is) — e.g. title-case, strip redundant
      codec/channel info already shown elsewhere in the badge. Cosmetic,
      not done — revisit if it turns out to matter in practice.
- [ ] Canonical-name table (like `app/languages.py`'s `LANGUAGE_OPTIONS`) —
      skipped; commentary/SDH turned out to need only two short pattern
      lists, not a lookup table.

## Suggested order

1. ~~Timezone fix (4)~~ — done.
2. ~~Per-track selective apply (1)~~ — done.
3. ~~Scheduler (3)~~ — done.
4. ~~Job queue (2)~~ — done.
5. ~~Track name normalization (5)~~ — mostly done (detection + new
   drop_hearing_impaired_tracks rule); display cleanup and a canonical-name
   table deliberately left open, not worth it yet.

All originally-planned items are now addressed in some form. Nothing left
queued unless something new comes up.
