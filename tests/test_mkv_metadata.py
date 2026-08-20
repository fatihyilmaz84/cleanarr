import subprocess
from pathlib import Path

import pytest

from app.mkv_metadata import MkvMetadataError, apply_metadata_changes, build_mkvpropedit_command, is_mkv
from app.normalizer import TrackNormalization


def _change(index=0, codec_type="audio", selector="a1", changed=True, new_title="English", new_default=None):
    return TrackNormalization(
        index=index,
        codec_type=codec_type,
        track_selector=selector,
        old_title=None,
        new_title=new_title,
        old_language="eng",
        new_language="eng",
        old_default=False,
        new_default=new_default,
        changed=changed,
        reason="test",
    )


def test_is_mkv_checks_extension():
    assert is_mkv(Path("movie.mkv")) is True
    assert is_mkv(Path("movie.MKV")) is True
    assert is_mkv(Path("movie.mp4")) is False


def test_build_command_sets_title_for_changed_track():
    cmd = build_mkvpropedit_command(Path("movie.mkv"), [_change()])
    assert cmd[0] == "mkvpropedit"
    assert cmd[1] == "movie.mkv"
    assert "--edit" in cmd
    edit_idx = cmd.index("--edit")
    assert cmd[edit_idx + 1] == "track:a1"
    assert "--set" in cmd
    assert "name=English" in cmd


def test_build_command_skips_unchanged_tracks():
    changed = _change(index=0, selector="a1")
    unchanged = _change(index=1, selector="a2", changed=False)
    cmd = build_mkvpropedit_command(Path("movie.mkv"), [changed, unchanged])
    assert "track:a1" in cmd
    assert "track:a2" not in cmd


def test_build_command_sets_default_flag_when_it_changes():
    cmd = build_mkvpropedit_command(Path("movie.mkv"), [_change(new_default=True)])
    assert "flag-default=1" in cmd


def test_build_command_raises_when_nothing_changed():
    with pytest.raises(MkvMetadataError, match="no changed tracks"):
        build_mkvpropedit_command(Path("movie.mkv"), [_change(changed=False)])


def test_apply_metadata_changes_runs_mkvpropedit(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    count = apply_metadata_changes(Path("movie.mkv"), [_change()])
    assert count == 1
    assert len(calls) == 1
    assert calls[0][0] == "mkvpropedit"


def test_apply_metadata_changes_noop_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    count = apply_metadata_changes(Path("movie.mkv"), [_change(changed=False)])
    assert count == 0


def test_apply_metadata_changes_raises_on_failure(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MkvMetadataError, match="mkvpropedit failed"):
        apply_metadata_changes(Path("movie.mkv"), [_change()])


def test_apply_metadata_changes_raises_when_binary_missing(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MkvMetadataError, match="not found"):
        apply_metadata_changes(Path("movie.mkv"), [_change()])


def test_a_detected_language_is_written_as_a_language_tag():
    """The whole point of detection: without --set language= the track stays
    unidentifiable to every other player, however nicely it is titled.
    """
    from pathlib import Path

    from app.mkv_metadata import build_mkvpropedit_command
    from app.normalizer import TrackNormalization

    change = TrackNormalization(
        index=1,
        codec_type="subtitle",
        track_selector="s1",
        old_title=None,
        new_title="Dutch",
        old_language=None,
        new_language="dut",
        old_default=False,
        new_default=None,
        changed=True,
        reason="language identified from the track's own text as 'dut'",
    )

    cmd = build_mkvpropedit_command(Path("movie.mkv"), [change])

    assert "--edit" in cmd and "track:s1" in cmd
    assert "name=Dutch" in cmd
    assert "language=dut" in cmd
