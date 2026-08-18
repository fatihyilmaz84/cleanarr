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

**Follow-up (time window + queue-draining)**: what happens with a large
batch and a schedule like "04:00-06:00"? — addressed:
- [x] `Schedule.end_hour`/`end_minute` (optional) define a run window.
      `window_deadline()` (`app/scheduler.py`) computes the deadline via
      `trigger + duration` (correct across a DST transition inside the
      window), treating `end <= start` as spanning past midnight (e.g.
      23:00-02:00). Both None (the default) means "no limit," unchanged
      from before.
- [x] The deadline is checked *between* files only, in both `run_scan`
      (`app/scanner.py`) and the shared apply loop
      (`app/actions.py::_apply_changes`) — never mid-file, since aborting a
      remux partway through could leave a corrupt temp file. A run that
      hits the deadline just stops there; whatever's left (unscanned files,
      unapplied changes) carries over to the next scheduled run or a manual
      scan / Run Queue. `ScanSummary.stopped_early` / the apply result's
      `stopped_early` and the job message make this visible via `/api/jobs`
      — there's no persistent history of past scheduled runs beyond that
      (no job-history page exists yet); the Queue page's own item count is
      the practical way to notice a run didn't finish everything.
- [x] New `apply_queued` schedule option, deliberately **separate** from
      `auto_apply` rather than folded into it: applies whatever's already
      sitting in the Queue (status=approved, i.e. already manually
      reviewed) — safe on its own terms since a human already confirmed
      those specific changes, unlike `auto_apply`'s "no review at all."
      Keeping them independent preserves the existing safety promise that
      `auto_apply=False` means "this schedule applies nothing unattended."
      A schedule can combine both; the two id sets can never overlap
      (a PendingChange has exactly one status at a time), so nothing is
      double-applied.
- Edge cases considered: a single file that takes longer than the whole
  window is still let to finish (documented in the Schedule page's own
  copy, not just a footnote) — a very short window paired with slow
  remuxes may mean slow overall progress across many nights, which is
  expected, not a bug. Zero-length/incomplete end-time input from the form
  is normalized to "no window" rather than erroring.

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

## 6. Language flag icons (FlagKit)

Show a small flag icon next to language names instead of/alongside plain
text — [FlagKit](https://github.com/madebybowtie/FlagKit) (MIT-licensed
SVG/PNG country flags) as the icon source, requested directly.

- [ ] Vendor a subset of FlagKit's SVGs into `app/static/` (self-hosted —
      this app has no external CDN dependency anywhere else, e.g. the
      Tailwind/font `<script>`/`<link>` tags in `base.html` are the only
      current exception and even those are 3rd-party CDN calls worth
      revisiting separately). No need for the full ~200-flag set — only
      the languages actually in `app/languages.py`'s `LANGUAGE_OPTIONS`
      (~57 entries).
- [ ] **Open design question**: FlagKit is *country* flags; this app deals
      in *languages*, and the mapping isn't 1:1 (English -> which flag,
      US or UK? Chinese -> which of several?). Needs an explicit
      language-code -> flag-code table (small, hand-picked, alongside
      `LANGUAGE_OPTIONS`) rather than a guessed convention — ask the user
      for preferences on the genuinely ambiguous ones rather than
      guessing silently.
- [ ] Surface it in: the Rules page's audio/subtitle `<select multiple>`
      dropdowns (`app/templates/rules.html`), the original-language badge
      and per-track language badges on Review/Queue
      (`app/templates/review.html`, `queue.html`), and the Review filter's
      "Original language" `<select>` (`app/web.py`'s `ui_review`).
- [ ] Keep it a pure visual enhancement — flags are decoration alongside
      the existing text, never a replacement for it (accessibility, and
      several languages share a plausible flag).

## 7. Track metadata normalizer (Jellyfin + Plex naming/metadata consistency)

Full spec supplied by the user 2026-08-19 — copied in spirit below, broken
into sub-tasks with notes on what already exists vs. what's genuinely new.
This is a substantially bigger feature than #5 above (which only decides
*keep vs. drop*) — this one *rewrites* track titles/language/disposition
metadata to a consistent scheme, for both Jellyfin and Plex.

### Architecture — resolved 2026-08-19
- **Separate menu item**, not folded into Rules/Review/Queue — its own nav
  entry and page(s), own settings, own review-style flow. Reuse
  Review/Queue's *pattern* (propose -> show before/after -> approve/queue
  -> apply) rather than the same pages, since it's a conceptually distinct
  operation (rename/retag vs. drop).
- Can run **standalone or alongside** the existing rule-based remover —
  neither requires the other to be configured.
- **Per-track mutual exclusion when both are active on the same file**:
  - A track the rule engine (`app/rules.py::decide`) has decided to
    **drop** is automatically out of scope for normalization — this falls
    out naturally from remuxing (`-metadata`/`-disposition` can only be
    set on a stream that's still `-map`ped into the output; a dropped
    stream doesn't exist in the result to retag), so this direction needs
    no extra coordination logic, just correct ordering: resolve drop
    decisions first, normalize only what survives.
  - The reverse needs real logic: a track a user has explicitly selected
    for normalization must be **protected from removal**, overriding
    whatever the rule engine would otherwise do. This is the same shape
    as `app/rules.py::apply_overrides` from #1 (force a decision from drop
    to keep) — reuse that mechanism (or a sibling of it keyed the same
    way, by stream index) rather than inventing new coordination state.
  - Net effect: "marked for normalization" implies "keep," decided before
    the drop pass finalizes, not layered on after.

### What already exists and this should build on, not duplicate
- Language canon + codes: `app/languages.py` (`LANGUAGE_OPTIONS`,
  `iso_codes_for_language_name`) already covers the "eng -> English",
  "use ISO metadata" pieces for ~57 languages.
- Forced detection: `MediaStream.is_forced` (ffprobe disposition), already
  used in `app/rules.py`.
- SDH/HI detection: `MediaStream.is_hearing_impaired` (disposition) +
  `RuleConfig.hearing_impaired_title_patterns` title-text fallback (#5,
  just shipped) — CC is not currently distinguished from SDH, would need
  its own flag/pattern list alongside it.
- Commentary detection: `is_commentary` (disposition) +
  `RuleConfig.commentary_title_patterns` (#5).
- Default-flag handling: not currently touched by `app/remux.py` at
  all — needs verification that ffmpeg's remux preserves (or lets us set)
  `disposition:default` explicitly rather than leaving it to chance.
- Dry-run / confirm-before-modifying: already the app's entire model —
  Review -> Queue -> Run Queue is inherently a dry-run-then-confirm flow,
  nothing proposed is ever silently applied. The spec's "BEFORE -> AFTER
  table" maps directly onto the existing Review Queue UI, just needs a
  title-rename row added to it (see below).
- Change log: `HistoryEntry` (`app/models.py`) already records what was
  removed per file; would need a `titles_changed`-style field alongside
  `streams_removed` for rename operations.

### Genuinely new work
- [ ] **New nav item + page(s)** — `app/templates/base.html`'s nav_link
      list gets a "Normalizer" entry (own icon/section, likely under
      "Library" alongside Review/Queue/History) and its own
      propose -> approve -> apply page flow, separate from Rules/Review.
- [ ] **Title/metadata rewriting itself** — `app/remux.py`'s
      `build_ffmpeg_command` only ever does `-map` (keep/drop); it never
      sets `-metadata:s:i:title=...`, `-metadata:s:i:language=...`, or
      `-disposition:s:i:...`. This is the core new capability everything
      else depends on.
      **Performance note, resolved 2026-08-19**: a pure-ffmpeg approach
      (`-c copy` + new `-metadata`/`-disposition` flags) is just as slow as
      today's track removal — it's still a full read+write of the whole
      file, since `-c copy` never skips the I/O, only the re-encode. For
      **MKV specifically**, prefer `mkvpropedit` (MKVToolNix) instead: it
      rewrites the Matroska header/track-metadata section in place,
      without touching the multi-GB media payload at all — near-instant
      regardless of file size, vs. minutes for a full remux. Modern
      Matroska has native fields for exactly what this needs
      (`FlagOriginal`, `FlagCommentary`, `FlagHearingImpaired`, track
      name, language), no remux required for pure metadata changes. New
      system dependency (`mkvtoolnix` package, alongside `ffmpeg` in the
      Dockerfile) but a small one. Doesn't help non-MKV containers
      (MP4/MOV don't guarantee in-place header edits the same way) — use
      the ffmpeg remux path as the fallback for those, and note the speed
      difference to the user per-file so a mixed-format library doesn't
      look inconsistently slow for no visible reason.
- [ ] Per-track normalization-vs-removal exclusivity (see Architecture
      above) — needs the drop pass resolved and any normalize-selected
      tracks force-kept *before* the final `-map`/`-metadata` ffmpeg
      command (or `mkvpropedit` call) is assembled, so the two operations
      combine into one pass rather than requiring two separate file
      rewrites.
- [ ] Naming-style setting (`Language - Attribute` vs `Language Attribute`
      vs bracketed, etc.) as a new `RuleConfig`/settings field, applied
      library-wide — one canonical scheme, not per-file.
- [ ] Per-category preserve/strip toggles: preserve meaningful existing
      titles, strip codec/channel/bitrate info, preserve
      Original/Dubbed/Commentary/Descriptive-Audio labels.
- [ ] Preferred-language auto-default selection (audio + subtitle,
      separately configurable) — with "never put 'Default' in the title
      itself, it's disposition metadata" as a hard rule.
- [ ] Forced/Foreign/Forced Narrative/Signs & Songs equivalence — opt-in
      only, off means treated as genuinely distinct (matches this app's
      existing philosophy of never assuming an aggressive default).
- [ ] Detection priority order (container metadata > track language >
      forced/default/SDH flags > existing title > filename > external
      convention) — already the shape of #5's disposition-then-title-regex
      approach; extend the same pattern rather than inventing a new one.
- [ ] Ambiguous-track flagging, separate from confident matches, in the
      Review UI.
- [ ] **Jellyfin vs. Plex research** — needs actual investigation (not
      guessing) into where the two disagree on interpreting
      forced/default/SDH container metadata, particularly for MKV. Until
      that's done, "apply the safest common representation + report the
      incompatibility, never silently discard" can't be implemented
      correctly.
- [ ] Target-server setting (Jellyfin / Plex / Both) — per the spec, avoid
      generating different metadata for the two unless the research above
      finds a real conflict; default to whatever's provably safe for both.

### Suggested approach when this gets picked up
Land the ffmpeg metadata-rewriting capability and a single naming
convention first (small, testable in isolation via `scripts/cli.py`
against real files, same as the original remux work) before layering the
Jellyfin/Plex-specific research and the dual-purpose Review UI on top —
this is large enough to warrant its own multi-session pass, not a single
sitting.

## Suggested order

1. ~~Timezone fix (4)~~ — done.
2. ~~Per-track selective apply (1)~~ — done.
3. ~~Scheduler (3)~~ — done (including the time-window/queue-draining
   follow-up).
4. ~~Job queue (2)~~ — done.
5. ~~Track name normalization (5)~~ — mostly done (detection + new
   drop_hearing_impaired_tracks rule); display cleanup and a canonical-name
   table deliberately left open, not worth it yet.
6. **Flag icons (6)** — small, self-contained, mostly a vendoring +
   language-to-flag mapping exercise.
7. **Track metadata normalizer (7)** — the big one; needs the scope
   clarification above resolved first, then the ffmpeg metadata-rewrite
   capability before anything else in that section can land.
