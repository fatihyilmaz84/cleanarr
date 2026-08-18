"""Standalone CLI to exercise the core engine (analyzer -> rules -> remux)
against real files, with no API/UI/DB involved. This is the Phase 1
validation tool: point it at a couple of real media files first with no
--apply (pure read-only preview), confirm the proposed drops look right,
then re-run with --apply on a throwaway copy before ever trusting it against
the real library.

Usage:
  python scripts/cli.py --rules rules.json /path/to/movie.mkv
  python scripts/cli.py --rules rules.json --apply /path/to/library/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyzer import AnalyzerError, probe_file
from app.remux import RemuxError, apply_remux
from app.rules import RuleConfig, decide

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}


def iter_media_files(target: Path):
    if target.is_file():
        yield target
        return
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            yield path


def load_rules(rules_path: Path | None) -> RuleConfig:
    if rules_path is None:
        return RuleConfig()
    return RuleConfig.model_validate_json(rules_path.read_text())


def describe(stream) -> str:
    bits = [stream.codec_type, stream.codec_name]
    if stream.language:
        bits.append(stream.language)
    if stream.title:
        bits.append(f'"{stream.title}"')
    flags = []
    if stream.is_default:
        flags.append("default")
    if stream.is_forced:
        flags.append("forced")
    if stream.is_commentary:
        flags.append("commentary")
    if flags:
        bits.append(f"[{','.join(flags)}]")
    return " ".join(bits)


def process_file(path: Path, config: RuleConfig, apply: bool) -> None:
    print(f"\n=== {path} ===")
    try:
        probe = probe_file(path)
    except AnalyzerError as e:
        print(f"  ERROR probing file: {e}")
        return

    decisions = decide(probe, config)
    dropped = [d for d in decisions if not d.keep]

    for d in decisions:
        mark = "KEEP" if d.keep else "DROP"
        print(f"  [{mark}] #{d.stream.index} {describe(d.stream)} — {d.reason}")

    if not dropped:
        print("  (nothing to change)")
        return

    if not apply:
        print(f"  {len(dropped)} stream(s) would be dropped. Re-run with --apply to remux.")
        return

    try:
        result = apply_remux(path, decisions)
    except RemuxError as e:
        print(f"  ERROR applying remux: {e}")
        return

    if result.applied:
        saved = (result.bytes_before or 0) - (result.bytes_after or 0)
        print(f"  APPLIED — removed {len(result.streams_removed)} stream(s), reclaimed {saved / 1e6:.1f}MB")
    else:
        print(f"  not applied: {result.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="media file or directory to scan")
    parser.add_argument("--rules", type=Path, default=None, help="path to a RuleConfig JSON file")
    parser.add_argument("--apply", action="store_true", help="actually remux files (default: preview only)")
    args = parser.parse_args()

    config = load_rules(args.rules)
    print(f"Rules: {json.dumps(config.model_dump(), indent=2)}")
    if args.apply:
        print("\n*** --apply is set: matching files WILL be modified in place. ***")

    for path in iter_media_files(args.target):
        process_file(path, config, args.apply)


if __name__ == "__main__":
    main()
