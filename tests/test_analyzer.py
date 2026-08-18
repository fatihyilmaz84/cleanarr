import json
import subprocess
from pathlib import Path

import pytest

from app.analyzer import AnalyzerError, MediaStream, probe_file

FFPROBE_SAMPLE = {
    "format": {"duration": "5413.184000"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {}, "disposition": {"default": 1}},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "tags": {"language": "eng"},
            "disposition": {"default": 1},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"LANGUAGE": "jpn", "title": "Director Commentary"},
            "disposition": {"comment": 1},
        },
        {
            "index": 3,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "und"},
            "disposition": {"forced": 1},
        },
    ],
}


def _mock_run(monkeypatch, stdout: str, returncode: int = 0, stderr: str = ""):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_probe_file_parses_streams(monkeypatch, tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"fake")
    _mock_run(monkeypatch, json.dumps(FFPROBE_SAMPLE))

    probe = probe_file(f)

    assert probe.duration_seconds == pytest.approx(5413.184)
    assert len(probe.streams) == 4
    assert len(probe.video_streams) == 1
    assert len(probe.audio_streams) == 2
    assert len(probe.subtitle_streams) == 1

    eng_audio = probe.audio_streams[0]
    assert eng_audio.language == "eng"
    assert eng_audio.is_default is True

    jpn_audio = probe.audio_streams[1]
    assert jpn_audio.language == "jpn"  # case-insensitive tag lookup
    assert jpn_audio.title == "Director Commentary"
    assert jpn_audio.is_commentary is True

    sub = probe.subtitle_streams[0]
    assert sub.language is None  # "und" normalized to None
    assert sub.is_forced is True


def test_probe_file_missing_file_raises(tmp_path):
    with pytest.raises(AnalyzerError):
        probe_file(tmp_path / "does-not-exist.mkv")


def test_probe_file_ffprobe_error_raises(monkeypatch, tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"fake")
    _mock_run(monkeypatch, stdout="", returncode=1, stderr="Invalid data found")

    with pytest.raises(AnalyzerError, match="ffprobe failed"):
        probe_file(f)


def test_probe_file_invalid_json_raises(monkeypatch, tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"fake")
    _mock_run(monkeypatch, stdout="not json")

    with pytest.raises(AnalyzerError, match="invalid JSON"):
        probe_file(f)


def test_media_stream_from_ffprobe_defaults():
    s = MediaStream.from_ffprobe_stream({"index": 0, "codec_type": "audio", "codec_name": "aac"})
    assert s.language is None
    assert s.title is None
    assert s.is_default is False
    assert s.is_forced is False
