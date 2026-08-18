# Cleanarr TODO

Feature backlog from the 2026-08-18 session. Working through these one at a
time, not all at once — check items off as they land, in whatever order
makes sense (see "Suggested order" at the bottom for dependencies).

## 1. Per-track selective apply

Right now, approving a pending change applies *every* stream the rule engine
proposed dropping for that file — no way to say "drop the audio track but
keep the subtitle" for one specific file.

- [ ] Add an `overrides` column to `PendingChange` (list of stream indices
      the user force-kept despite the rule engine flagging them) — needs a
      migration via `app/db.py`'s `_add_missing_columns`, same pattern as
      `original_language`.
- [ ] Review Queue: render each proposed-drop stream with a checkbox
      ("drop this track", checked by default = today's behavior), inside
      the approve form.
- [ ] `POST /review/{id}/approve`: read which drop-checkboxes stayed
      checked, store the unchecked ones as `overrides`.
- [ ] `apply_pending_change` (`app/apply.py`): after re-deciding against the
      *current* rules (still don't trust the stale scan-time decision —
      the file/rules may have changed since queueing), force-flip any
      stream index in `overrides` back to `keep=True` before handing off
      to `apply_remux`.
- [ ] Same override support for the bulk-approve path
      (`/api/review/approve-bulk`, and whatever UI Task 2 adds).

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

## 3. Scheduler (e.g. "every night at 04:00, scan + apply")

- [ ] Depends on Task 4 (timezone fix) — "04:00" has to mean 04:00 in a
      well-defined timezone, or it'll be wrong from day one.
- [ ] New settings model: list of schedules — time of day, days of week,
      action (`scan`, `apply all pending`, or `scan + auto-apply matching
      a specific rule set`), enabled flag. Store via the existing
      `app_settings` key/value pattern (`app/settings_store.py`).
- [ ] Lightweight in-process scheduler: an asyncio task (started/stopped
      alongside `JobManager` in `app/main.py`'s lifespan) that wakes up
      once a minute, checks configured schedules against current
      *server-local* time, and calls `submit_scan_job` / `submit_apply_job`
      on a match. No new dependency (no APScheduler/cron lib) — consistent
      with the existing "deliberately minimal" job runner in `app/jobs.py`.
- [ ] Guard against double-firing within the same minute (track last-fired
      timestamp per schedule).
- [ ] New `/schedule` page to manage schedules.
- [ ] Decide: should a scheduled run auto-apply pending changes, or only
      auto-scan and leave everything for manual review? (Given the app's
      whole design principle is "nothing is ever applied automatically" —
      this needs an explicit, obvious opt-in per schedule, not a default.)

## 4. Fix timezone handling

Confirmed root cause: the Docker container has no `TZ` set (defaults to
UTC), and every timestamp (`MediaFile.last_scanned_at`, `PendingChange.
created_at`, `HistoryEntry.applied_at`, `Job.created_at`, etc.) is stored
via `datetime.now(timezone.utc)` in `app/models.py`. That's correct and
unambiguous internally, but templates render it raw:
- `app/templates/overview.html:38` — `h.applied_at.strftime(...)`
- `app/templates/history.html:23` — `h.applied_at.strftime(...)`
  — both print the UTC wall-clock time with no conversion, so it looks
  "wrong" against the user's actual local time.

- [ ] Set an explicit `TZ` env var (e.g. `Europe/Berlin`) in the Dockerfile
      or docker-compose, so the container's local time actually matches
      the user's.
- [ ] Convert stored UTC datetimes to local time at display time (e.g. a
      small Jinja filter `|localtime` using `astimezone()`), rather than
      relying on ambient container TZ alone — keeps storage unambiguous
      (UTC) while fixing what's shown.
- [ ] Decide what "local" means here: container TZ, or a configurable
      timezone in Settings (more correct if Cleanarr ever runs somewhere
      other than the user's own timezone).

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

1. **Timezone fix (4)** — small, self-contained, and a correctness
   prerequisite for the scheduler.
2. **Track name normalization (5)** — small, independent, improves rule
   matching quality before more automation (scheduler) starts relying on it.
3. **Per-track selective apply (1)** — moderate size, standalone value.
4. **Job queue (2)** — builds naturally on top of (1).
5. **Scheduler (3)** — biggest, benefits from (1)-(4) already being solid
   since it's the thing that'll run everything unattended.
