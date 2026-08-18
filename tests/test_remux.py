import shutil
import subprocess

import pytest

from app import remux as remux_mod
from app.remux import RemuxError, apply_remux, build_ffmpeg_command, preflight_check
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
