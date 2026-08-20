import shutil
import subprocess
import time

import pytest

from app import remux as remux_mod
from app.remux import RemuxError, apply_remux, build_ffmpeg_command, preflight_check, verify_output
from tests.fixtures import make_probe, make_stream

from app.rules import StreamDecision


def kept(stream):
    return StreamDecision(stream, True, "kept for test")


def dropped(stream, reason="dropped for test"):
    return StreamDecision(stream, False, reason)


def _decisions_drop_one():
    v = make_stream(0, "video")
    a1 = make_stream(1, "audio", language="eng", is_default=True)
    a2 = make_stream(2, "audio", language="jpn")
    return [kept(v), kept(a1), dropped(a2, "language 'jpn' not in keep-list")]


def test_build_ffmpeg_command_maps_only_kept_streams():
    decisions = _decisions_drop_one()
    cmd = build_ffmpeg_command(remux_mod.Path("in.mkv"), remux_mod.Path("out.mkv"), decisions)

    assert cmd[0] == "ffmpeg"
    assert "-map" in cmd
    mapped = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-map" and cmd[i + 1].startswith("0:")]
    assert mapped == ["0:0", "0:1"]
    assert "-c" in cmd and "copy" in cmd


def test_build_ffmpeg_command_raises_if_nothing_kept():
    v = make_stream(0, "video")
    with pytest.raises(RemuxError):
        build_ffmpeg_command(remux_mod.Path("in.mkv"), remux_mod.Path("out.mkv"), [dropped(v)])


def test_preflight_check_raises_when_low_on_space(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)

    class FakeUsage:
        free = 500  # less than file size + buffer

    monkeypatch.setattr(shutil, "disk_usage", lambda path: FakeUsage())

    with pytest.raises(RemuxError, match="insufficient free space"):
        preflight_check(f)


def test_preflight_check_passes_when_enough_space(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)

    class FakeUsage:
        free = 10 * 1024 * 1024 * 1024

    monkeypatch.setattr(shutil, "disk_usage", lambda path: FakeUsage())
    preflight_check(f)  # should not raise


def _fake_ffmpeg_writes_output(output_bytes: bytes):
    def fake_run(cmd, capture_output, text, timeout):
        out_path = remux_mod.Path(cmd[-1])
        out_path.write_bytes(output_bytes)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_apply_remux_nothing_to_drop_short_circuits(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"original")

    monkeypatch.setattr(remux_mod, "probe_file", lambda path, ffprobe_bin="ffprobe": make_probe([kept(make_stream(0, "video")).stream]))

    decisions = [kept(make_stream(0, "video"))]
    result = apply_remux(f, decisions)

    assert result.applied is False
    assert f.read_bytes() == b"original"  # untouched


def test_apply_remux_success_replaces_file_atomically(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"original-bytes-that-are-longer")

    decisions = _decisions_drop_one()
    kept_streams = [d.stream for d in decisions if d.keep]

    call_state = {"n": 0}

    def fake_probe(path, ffprobe_bin="ffprobe"):
        call_state["n"] += 1
        if call_state["n"] == 1:
            # original probe, called before remux
            return make_probe([d.stream for d in decisions], duration_seconds=100.0)
        # verification probe against the remuxed temp file
        return make_probe(kept_streams, duration_seconds=100.5)

    monkeypatch.setattr(remux_mod, "probe_file", fake_probe)
    monkeypatch.setattr(subprocess, "run", _fake_ffmpeg_writes_output(b"short"))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 10 * 1024**3})())

    result = apply_remux(f, decisions)

    assert result.applied is True
    assert f.read_bytes() == b"short"  # original replaced with remuxed output
    assert result.bytes_before == len(b"original-bytes-that-are-longer")
    assert result.bytes_after == len(b"short")
    assert len(result.streams_removed) == 1
    assert result.streams_removed[0]["language"] == "jpn"
    assert not (f.parent / f".cleanarr.tmp.{f.name}").exists()  # tmp cleaned up


def test_apply_remux_reports_progress_via_output_growth(tmp_path, monkeypatch):
    # Simulates a slow "-c copy" by growing the tmp output file in two steps
    # with a pause in between, so the background size-watcher thread has a
    # real chance to observe a partial size before ffmpeg "finishes".
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)

    decisions = _decisions_drop_one()
    kept_streams = [d.stream for d in decisions if d.keep]
    call_state = {"n": 0}

    def fake_probe(path, ffprobe_bin="ffprobe"):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return make_probe([d.stream for d in decisions], duration_seconds=100.0)
        return make_probe(kept_streams, duration_seconds=100.0)

    def fake_run(cmd, capture_output, text, timeout):
        out_path = remux_mod.Path(cmd[-1])
        out_path.write_bytes(b"x" * 400)  # partial write, ~40% of original size
        time.sleep(1.2)  # give the watcher thread (1s poll interval) a chance to sample it
        out_path.write_bytes(b"x" * 900)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(remux_mod, "probe_file", fake_probe)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 10 * 1024**3})())

    observed: list[float] = []
    result = apply_remux(f, decisions, progress_cb=observed.append)

    assert result.applied is True
    assert observed[-1] == 1.0  # final call always signals true completion
    assert any(0 < v < 1.0 for v in observed), observed  # at least one real partial sample
    assert all(v <= 1.0 for v in observed)


def test_apply_remux_ffmpeg_failure_leaves_original_untouched(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"original")

    decisions = _decisions_drop_one()

    monkeypatch.setattr(remux_mod, "probe_file", lambda path, ffprobe_bin="ffprobe": make_probe([d.stream for d in decisions], 100.0))
    monkeypatch.setattr(subprocess, "run", lambda cmd, capture_output, text, timeout: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 10 * 1024**3})())

    with pytest.raises(RemuxError, match="ffmpeg failed"):
        apply_remux(f, decisions)

    assert f.read_bytes() == b"original"
    assert not (f.parent / f".cleanarr.tmp.{f.name}").exists()


def test_apply_remux_verification_failure_leaves_original_untouched(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"original")

    decisions = _decisions_drop_one()

    call_state = {"n": 0}

    def fake_probe(path, ffprobe_bin="ffprobe"):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return make_probe([d.stream for d in decisions], 100.0)
        # wrong stream count -> verification should fail
        return make_probe([decisions[0].stream], 100.0)

    monkeypatch.setattr(remux_mod, "probe_file", fake_probe)
    monkeypatch.setattr(subprocess, "run", _fake_ffmpeg_writes_output(b"short"))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 10 * 1024**3})())

    with pytest.raises(RemuxError, match="verification failed"):
        apply_remux(f, decisions)

    assert f.read_bytes() == b"original"
    assert not (f.parent / f".cleanarr.tmp.{f.name}").exists()


# --- verify_output -----------------------------------------------------
#
# Both cases below are taken from real files on a live library that the
# original stream-count/container-duration checks rejected even though the
# remux was correct — see the failure messages quoted in each test.


def _verified(monkeypatch, original, decisions, output_streams, output_duration):
    """Run verify_output against a faked probe of the remuxed file."""
    monkeypatch.setattr(
        remux_mod, "probe_file", lambda path, ffprobe_bin=None: make_probe(output_streams, duration_seconds=output_duration)
    )
    verify_output(original, remux_mod.Path("out.mkv"), decisions)


def test_verify_output_ignores_a_chapter_track_the_muxer_added_itself(monkeypatch):
    # Real case: "expected 4 streams, remuxed file has 5" — the mp4 muxer
    # emits its own text/bin_data chapter track whenever the input has
    # chapters, which was never in our -map list.
    v = make_stream(0, "video", codec_name="h264", language=None)
    a = make_stream(1, "audio", codec_name="ac3", language="eng")
    s = make_stream(2, "subtitle", codec_name="mov_text", language="eng")
    decisions = [kept(v), kept(a), kept(s)]

    output = [v, a, s, make_stream(3, "data", codec_name="bin_data", language=None)]
    _verified(monkeypatch, make_probe([v, a, s], duration_seconds=100.0), decisions, output, 100.0)


def test_verify_output_rejects_a_kept_track_that_went_missing(monkeypatch):
    v = make_stream(0, "video", codec_name="h264", language=None)
    a_eng = make_stream(1, "audio", codec_name="ac3", language="eng")
    a_jpn = make_stream(2, "audio", codec_name="ac3", language="jpn")
    decisions = [kept(v), kept(a_eng), dropped(a_jpn)]

    with pytest.raises(RemuxError, match="missing audio/ac3/eng"):
        _verified(monkeypatch, make_probe([v, a_eng, a_jpn], duration_seconds=100.0), decisions, [v], 100.0)


def test_verify_output_rejects_a_dropped_track_that_survived(monkeypatch):
    v = make_stream(0, "video", codec_name="h264", language=None)
    a_eng = make_stream(1, "audio", codec_name="ac3", language="eng")
    a_jpn = make_stream(2, "audio", codec_name="ac3", language="jpn")
    decisions = [kept(v), kept(a_eng), dropped(a_jpn)]

    with pytest.raises(RemuxError, match="unexpected audio/ac3/jpn"):
        _verified(monkeypatch, make_probe([v, a_eng, a_jpn], duration_seconds=100.0), decisions, [v, a_eng, a_jpn], 100.0)


def test_verify_output_accepts_the_container_shrinking_when_the_longest_track_is_dropped(monkeypatch):
    # Real case (WALL·E): "duration changed by 29.2s (original 5921.8s,
    # remuxed 5892.6s)". The container was only 5921.8s long because of a
    # Spanish subtitle track running past the credits — which this run
    # drops. The video is 5891.9s; 5892.6s out is exactly right.
    v = make_stream(0, "video", codec_name="av1", language=None, duration_seconds=5891.9)
    a = make_stream(1, "audio", codec_name="opus", language="eng", duration_seconds=5892.6)
    s_spa = make_stream(2, "subtitle", codec_name="subrip", language="spa", duration_seconds=5921.8)
    decisions = [kept(v), kept(a), dropped(s_spa)]

    _verified(monkeypatch, make_probe([v, a, s_spa], duration_seconds=5921.8), decisions, [v, a], 5892.6)


def test_verify_output_still_catches_a_truncated_remux(monkeypatch):
    # The check must stay strict about the case it exists for: output that
    # is genuinely shorter than the tracks being kept.
    v = make_stream(0, "video", codec_name="av1", language=None, duration_seconds=5891.9)
    a = make_stream(1, "audio", codec_name="opus", language="eng", duration_seconds=5892.6)
    s_spa = make_stream(2, "subtitle", codec_name="subrip", language="spa", duration_seconds=5921.8)
    decisions = [kept(v), kept(a), dropped(s_spa)]

    with pytest.raises(RemuxError, match="duration changed"):
        _verified(monkeypatch, make_probe([v, a, s_spa], duration_seconds=5921.8), decisions, [v, a], 4000.0)


def test_verify_output_falls_back_to_container_duration_when_tracks_have_none(monkeypatch):
    v = make_stream(0, "video", codec_name="h264", language=None)
    a = make_stream(1, "audio", codec_name="ac3", language="eng")
    a_jpn = make_stream(2, "audio", codec_name="ac3", language="jpn")
    decisions = [kept(v), kept(a), dropped(a_jpn)]

    with pytest.raises(RemuxError, match="duration changed"):
        _verified(monkeypatch, make_probe([v, a, a_jpn], duration_seconds=100.0), decisions, [v, a], 40.0)
