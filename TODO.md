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

### 3b. Attach rules to a schedule, for cleaning *and* normalizing — done ✅

Two gaps this closed: a schedule could only ever run the rule-based
cleaner (the normalizer's whole pipeline was unreachable from a schedule),
and rules were a single global singleton with no way to vary them per
schedule.

- [x] `RulePreset` / `NormalizerPreset` (`app/settings_store.py`): named,
      saved configs stored under their own settings keys. The Rules /
      Normalize Settings pages' own configs stay the unnamed **Default**,
      used by manual Scan Now and by any schedule with no preset attached —
      so this is purely additive and nothing needed migrating.
- [x] Preset CRUD reuses the existing edit form via one-form-two-targets:
      `/rules` edits Default, `/rules?preset=<id>` edits that preset. Same
      shape for `/normalize/settings`. A new preset is seeded from Default
      rather than from empty, so it can never start life as an inert
      "keep nothing" config that a schedule quietly attaches to.
- [x] `Schedule` gained `run_clean` / `run_normalize` (what to run) plus
      `rule_preset_id` / `normalizer_preset_id` (what to run it with), and
      a second pair of apply opt-ins for the normalizer
      (`normalize_auto_apply` / `normalize_apply_queued`) rather than
      reusing the cleaner's — they're independent systems with different
      risk profiles. Defaults (`run_clean=True`, `run_normalize=False`)
      mean an existing schedule keeps doing exactly what it did.
- [x] **The correctness property this rests on:** `app/apply.py` and
      `app/normalize_service.py` both deliberately re-decide from scratch
      at apply time rather than trusting the cached `proposed`. So the
      config used at apply time *must* be the one that proposed the
      change — otherwise what gets dropped wouldn't match what the user
      was shown and approved. Solved by stamping
      `PendingChange.rule_preset_id` / `NormalizationChange.
      normalizer_preset_id` at propose time and resolving per-row at apply
      time. Covered by a test that was verified to fail (stripping the
      wrong audio track) when the resolution is removed.
- [x] Deleting a preset is never destructive: `resolve_rule_config` /
      `resolve_normalizer_config` fall back to Default for an unknown id,
      so a stale reference can't wedge a scheduled run or block an
      already-queued change. The Schedule list renders such a reference as
      "deleted preset → Default" rather than a stale name or a bare id.
- [x] The normalizer gained the deadline support the cleaner already had
      (`propose_normalizations(deadline=...)` plus a between-files check in
      the normalize apply loop), so a windowed schedule bounds both halves.
      It also gained `NormalizeScanSummary.change_ids` so a scheduled
      normalize auto-apply is scoped to that pass's own findings, mirroring
      `ScanSummary.pending_change_ids`.
- [x] Fixed in passing: `propose_normalizations` only matched
      `status=pending` when looking for an existing change, so re-proposing
      over an already-queued (approved) file created a **duplicate** row —
      the same bug previously fixed in `app/scanner.py`.
- Both jobs go on the same single-worker queue, so a schedule running both
  runs them sequentially, never concurrently; normalize is submitted second
  on purpose, so it sees whatever the scan just refreshed and skips tracks
  the scan just proposed dropping.
- [x] Editing a saved schedule reuses the same one-form-two-targets shape
      as the preset pages: `/schedule` adds, `/schedule?edit=<id>` edits
      that one in place. Both modes share one parser and one set of
      prefilled inputs (seeded from a default `Schedule()` when adding), so
      an edit can't drift from what an add accepts. The edit keeps the
      schedule's `id` and its enabled/disabled state — the id is what the
      toggle/delete buttons address and what the scheduler's
      fired-this-minute bookkeeping keys off, so a re-created one could
      make an edit mid-window re-fire a schedule that had already run.
- Fixed in passing: `_redirect` appended `?msg=` unconditionally, so a
  redirect back to a path that already had a query string (`?preset=`,
  now `?edit=`) folded the message into the preceding param's value —
  losing both the message and the param. The preset pages' "Preset saved"
  redirect had been silently bouncing to Default because of it.
- Still open: per-schedule media-path scoping ("clean only /movies on
  Sundays").

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

## 7. Track metadata normalizer (Jellyfin + Plex naming/metadata consistency) — MVP done ✅

Full spec supplied by the user 2026-08-19. This is a substantially bigger
feature than #5 above (which only decides *keep vs. drop*) — this one
*rewrites* track titles/language/default-flag metadata to a consistent
scheme. A working, tested MVP shipped 2026-08-19 covering the core
proposal; several spec items are deliberately deferred (see "Still open"
below) rather than block on the whole spec at once.

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

### What's built (2026-08-19)
- [x] **New nav item + pages** — `app/templates/base.html` gained a
      "Normalizer" nav section (Normalize / Normalize Queue) and a
      "Normalize Settings" entry under Configuration, entirely separate
      from Rules/Review/Queue. Three new templates
      (`normalize.html`/`normalize_queue.html`/`normalize_settings.html`)
      and a dedicated router (`app/normalize_web.py`, mounted in
      `main.py`) rather than folding into `app/web.py`, which was already
      large.
- [x] **Title/metadata rewriting via `mkvpropedit`** (`app/mkv_metadata.py`)
      — per the performance note below, MKV-only for now. Builds one
      `mkvpropedit --edit track:aN/sN/vN --set name=... --set
      language=... --set flag-default=...` invocation per file, only for
      tracks that actually changed. Track selectors are computed
      type-relative (`a1`, `s2`, ...) rather than using ffprobe's global
      stream index, since Matroska attachments/chapters can make bare
      positional numbering diverge between the two tools — a real
      correctness risk if gotten wrong (could silently retag the wrong
      track).
- [x] **Pure decision logic** (`app/normalizer.py`, mirrors `app/rules.py`'s
      split from I/O) — `NormalizerConfig` + `normalize_streams()`.
      Detects language (via `app/languages.py`, extended with a new
      `language_name_for_code()` reverse lookup), forced (disposition +
      opt-in Foreign/Forced Narrative/Signs & Songs equivalence patterns),
      SDH (disposition + title patterns), CC (title patterns, SDH takes
      priority if both somehow match), commentary, original, and dubbed
      (all title-pattern-based, same mechanism as #5's
      commentary/hearing-impaired patterns, but a fully separate
      `NormalizerConfig` instance — the two systems stay decoupled per the
      Architecture note above). Builds canonical titles in two naming
      styles (`English - SDH` / `English SDH`). Auto-default selection
      picks one track per type and explicitly clears the flag on every
      other candidate of that type (not just the chosen one), so exactly
      one ends up default — an early draft of this had a bug here, caught
      by `tests/test_normalizer.py`. An unrecognized/untagged language
      code is left untouched rather than guessed.
- [x] **Per-track exclusivity, implemented** — `app/normalize_service.py`'s
      `propose_normalizations()` excludes any stream index
      `app/rules.py`'s drop engine currently proposes removing for that
      file (checking `PendingChange.proposed`/`overrides`, both `pending`
      and `approved` status). The reverse (normalize-selected track
      protected from removal) is *not* wired up on the rules.py side yet
      — right now a track queued for normalization could still get
      dropped by a later scan if Rules independently decides to remove
      it. Follow-up, not done in this pass.
- [x] **DB-touching orchestration** (`app/normalize_service.py`) —
      `propose_normalizations()` reads already-scanned
      `MediaFile`/`StreamRecord` rows (no ffprobe re-run — the normalizer
      is a pure function of data the regular scan already collected) and
      upserts `NormalizationChange` rows (new table, mirrors
      `PendingChange`'s status machine: pending -> approved -> applied,
      or skipped/failed). `apply_normalization_change()` re-decides from
      scratch against current streams/config at apply time (never trusts
      the cached proposal, same reasoning as `app/apply.py`), applies
      per-track overrides, and writes the result back into `StreamRecord`
      so a future pass sees the file as already-normalized.
- [x] Job wiring (`app/actions.py`: `submit_normalize_scan_job`,
      `submit_normalize_apply_job`) reusing the existing `JobManager`/
      progress-bar infrastructure — new job kinds `normalize_scan` /
      `normalize_apply`.
- [x] **Naming-style, preserve/strip via canonical regeneration** — rather
      than editing existing titles in place (fuzzy "is this meaningful"
      judgment calls), the normalizer always *builds* a fresh canonical
      title from detected attributes. This sidesteps the spec's separate
      "preserve meaningful titles" / "strip technical info" toggles
      entirely — there's nothing to strip or preserve from, since
      codec/channel/bitrate info was never carried into the generated
      title to begin with. Simpler and more predictable than the
      toggle-based approach; revisit only if real libraries show a case
      where blanket regeneration loses something worth keeping.
- [x] `Dockerfile` installs `mkvtoolnix` (CLI package, no Qt/GUI deps)
      alongside `ffmpeg`.
- [x] Tests: `tests/test_normalizer.py` (21, pure logic incl. the
      auto-default bug above), `tests/test_mkv_metadata.py` (9, command
      building + subprocess mocking), `tests/test_normalize_service.py`
      (7, incl. both directions of the drop/normalize interaction),
      `tests/test_normalize_web.py` (6, full scan -> approve -> queue ->
      run flow).

### Still open (deliberately deferred, not forgotten)
- [ ] **Non-MKV fallback** — MP4/MOV/etc. currently just fail with "only
      MKV files are supported for normalization right now"
      (`apply_normalization_change`). An ffmpeg-remux fallback is
      possible but slow (see performance note) and wasn't worth blocking
      the MKV path on.
- [ ] **Reverse per-track exclusivity** — protecting a normalize-queued
      track from a *later* independent Rules decision to drop it (see
      above). Today the exclusion only runs at `propose_normalizations()`
      time, one direction.
- [ ] Ambiguous-track flagging, separate from confident matches, in the
      Normalize UI — everything today is either a confident proposal or
      silently left alone; no explicit "not sure" state.
- [ ] **Jellyfin vs. Plex research** — needs actual investigation (not
      guessing) into where the two disagree on interpreting
      forced/default/SDH container metadata. The current implementation
      writes standard Matroska fields (`flag-default`, track `name`,
      `language`) that both are expected to read the same way, but the
      spec's "apply the safest common representation + report the
      incompatibility" for cases where they *don't* agree isn't
      implemented — no known conflict has been identified yet to encode.
- [ ] Target-server setting (Jellyfin / Plex / Both) from the original
      spec — not added; everything written today is the
      provably-common-ground representation, so the setting has no
      effect to control yet. Add once the research above finds a real
      divergence worth switching on.
- [ ] Descriptive-audio detection ("Descriptive Audio" from the spec's
      examples) — not implemented; no title-pattern config for it yet.
- [ ] Filters on the Normalize page (search/library-type/etc., like
      Review gained) — not added in this pass, kept deliberately simple.

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
   language-to-flag mapping exercise. Still open.
7. ~~Track metadata normalizer (7)~~ — MVP done (MKV via mkvpropedit,
   separate menu item, per-track exclusion from the drop engine). Still
   open within it: non-MKV fallback, the reverse exclusion direction,
   Jellyfin/Plex divergence research, target-server setting, ambiguous-
   track UI, descriptive-audio detection, Normalize page filters.
