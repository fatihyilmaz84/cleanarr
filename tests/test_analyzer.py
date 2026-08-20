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


def test_matroska_track_duration_comes_from_the_tag_not_the_repeated_container_duration():
    # Matroska repeats the whole segment's duration in every stream's
    # `duration` field. A real library file had a subtitle track reporting
    # 6721.568s that way while its own DURATION tag said 01:50:24.052 —
    # trusting the former made the remux verifier expect an output as long
    # as the *dropped* audio track.
    s = MediaStream.from_ffprobe_stream(
        {
            "index": 3,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "duration": "6721.568000",
            "tags": {"language": "eng", "DURATION": "01:50:24.052000000"},
        },
        trust_stream_duration=False,
    )
    assert s.duration_seconds == pytest.approx(6624.052)


def test_matroska_track_with_no_duration_tag_is_unknown_rather_than_the_container_length():
    s = MediaStream.from_ffprobe_stream(
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "duration": "5422.432000"},
        trust_stream_duration=False,
    )
    assert s.duration_seconds is None


def test_mp4_track_duration_comes_from_the_stream_field():
    s = MediaStream.from_ffprobe_stream(
        {"index": 0, "codec_type": "video", "codec_name": "h264", "duration": "5603.597978"}
    )
    assert s.duration_seconds == pytest.approx(5603.597978)
