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

## 2. Job queue (queue-then-run, not apply-on-approve)

Today, "Approve & Apply" immediately submits an apply job. Wanted instead:
select multiple review items, queue them up, then hit one "Run Queue"
action that processes everything at once.

- [ ] Repurpose `ChangeStatus.approved` as the "queued, not yet run" state
      (it's already a distinct status — currently just auto-followed by an
      immediate `submit_apply_job` call in `app/web.py`'s `ui_approve`).
- [ ] Review Queue: checkboxes per item + a "Queue Selected" bulk action
      (the JSON API already has `/api/review/approve-bulk` for this — just
      needs marking status without submitting a job, and a web UI form).
- [ ] New `/queue` page: lists everything in `approved` status, with a
      "Run Queue (N)" button that calls `submit_apply_job` with all of
      their ids at once (this already supports a list of change_ids —
      `app/actions.py::submit_apply_job` — just needs a UI entry point).
- [ ] Let users remove an item from the queue (revert to `pending`) before
      running it.
- [ ] Depends on Task 1 if we want per-track overrides selectable *at
      queue time* rather than only at approve time.

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

## 5. Normalize track names across containers

Different muxers/tools tag the same kind of track differently (e.g. a
commentary track's `title` might be "Commentary", "Director's Commentary",
"commentary track", or nothing at all; hearing-impaired subs might be
"SDH", "English (SDH)", "Hearing Impaired", or rely solely on the
`disposition` flag).

- [ ] Audit `app/analyzer.py`'s `MediaStream.from_ffprobe_stream` — right
      now `is_commentary`/`is_hearing_impaired` come *only* from ffprobe's
      `disposition` dict. Add a title-text fallback (regex/keyword match,
      similar to the existing `drop_title_patterns` mechanism in
      `app/rules.py`) for containers that don't set disposition flags but
      do label it in the title.
- [ ] Normalize *display* of track titles in the Review Queue (currently
      raw `s.title` shown as-is in `app/templates/review.html`) — e.g.
      title-case, strip redundant codec/channel info already shown
      elsewhere in the badge.
- [ ] Consider a small canonical-name table (similar to
      `app/languages.py`'s `LANGUAGE_OPTIONS`) if a clear common pattern
      set emerges from real library data.

## Suggested order

1. ~~Timezone fix (4)~~ — done.
2. ~~Per-track selective apply (1)~~ — done.
3. ~~Scheduler (3)~~ — done.
4. **Job queue (2)** — builds naturally on top of (1)'s override UI.
5. **Track name normalization (5)** — small, independent; still open.
