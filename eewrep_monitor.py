#!/usr/bin/env python3
"""
Continuously convert eew .rep files to eewrep_summary.json and push to GitHub.

Designed to run inside a local clone of:
https://github.com/cwbdayi638/EEWS_GAS_Dashboard_Data

Default behavior:
  * scan the repository root for top-level *.rep files every 300 seconds;
  * parse complete files and merge their solutions into eewrep_summary.json;
  * archive each parsed file under YYYY_MM/;
  * commit only the summary and archived .rep files, then push origin/main;
  * skip a file whose basename already exists in the summary.

Run once for testing:
    python eewrep_monitor.py --once --repo-dir C:\\path\\to\\EEWS_GAS_Dashboard_Data

Run continuously:
    python eewrep_monitor.py --repo-dir C:\\path\\to\\EEWS_GAS_Dashboard_Data

Create .env in the repository root. GITHUB_TOKEN must be a GitHub Personal
Access Token (PAT), not the GitHub account password:
    GITHUB_USERNAME=your_github_username
    GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


LOG = logging.getLogger("eewrep_monitor")
DEFAULT_REPOSITORY = "https://github.com/cwbdayi638/EEWS_GAS_Dashboard_Data"
REPORT_RE = re.compile(
    r"Reporting\s+time\s+"
    r"(?P<date>\d{4}/\d{1,2}/\d{1,2})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
COUNT_RE = re.compile(r"\bn_c\s*=\s*(\d+)", re.IGNORECASE)
AUTHOR_RE = re.compile(r"\bauthor\s*=\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)
NUMBER_ROW_RE = re.compile(
    r"^\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+"
    r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2}(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)"
    r"(?:\s+([-+]?\d+(?:\.\d+)?))?"
    r"(?:\s+([-+]?\d+(?:\.\d+)?))?\s*$"
)


class RepParseError(ValueError):
    """Raised when a .rep file is incomplete or not in the expected format."""


@dataclass(frozen=True)
class ParsedRep:
    path: Path
    report_time: datetime
    origin_time: datetime
    lat: float
    lon: float
    depth: float
    magnitude_mpd: float
    station_count: int
    author: str


def parse_datetime(date_text: str, time_text: str) -> datetime:
    return datetime.strptime(f"{date_text} {time_text}", "%Y/%m/%d %H:%M:%S.%f")


def iso_hundredths(value: datetime) -> str:
    """Match the existing JSON style, e.g. 2026-07-28T08:44:22.25."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 10000:02d}"


def time_hundredths(value: datetime) -> str:
    return value.strftime("%H:%M:%S.") + f"{value.microsecond // 10000:02d}"


def parse_rep(path: Path) -> ParsedRep:
    text = path.read_text(encoding="utf-8", errors="replace")
    report_match = REPORT_RE.search(text)
    if not report_match:
        raise RepParseError("missing 'Reporting time'")

    report_time = parse_datetime(report_match["date"], report_match["time"])
    count_match = COUNT_RE.search(report_match["rest"])
    station_count = int(count_match.group(1)) if count_match else 0
    author_match = AUTHOR_RE.search(report_match["rest"])
    author = author_match.group(1) if author_match else "params"

    row: re.Match[str] | None = None
    for line in text.splitlines():
        candidate = NUMBER_ROW_RE.match(line)
        if candidate:
            row = candidate
            break
    if not row:
        raise RepParseError("missing earthquake parameter row")

    values = row.groups()
    origin_time = datetime(
        int(values[0]),
        int(values[1]),
        int(values[2]),
        int(values[3]),
        int(values[4]),
        int(float(values[5])),
        round((float(values[5]) % 1) * 1_000_000),
    )
    # Columns after origin time:
    # lat lon dep Mall Mpd_s Mpv Mpd Mtc [process_time] [first_ptime]
    return ParsedRep(
        path=path,
        report_time=report_time,
        origin_time=origin_time,
        lat=float(values[6]),
        lon=float(values[7]),
        depth=float(values[8]),
        magnitude_mpd=float(values[12]),
        station_count=station_count,
        author=author,
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def find_event(
    events: list[dict[str, Any]],
    rep: ParsedRep,
    time_tolerance_sec: float,
    distance_tolerance_km: float,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for event in events:
        try:
            event_time = parse_iso(str(event["origin_time"]))
            delta = abs((rep.origin_time - event_time).total_seconds())
            distance = haversine_km(
                rep.lat, rep.lon, float(event["lat"]), float(event["lon"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if delta <= time_tolerance_sec and distance <= distance_tolerance_km:
            candidates.append((delta, distance, event))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def make_event(rep: ParsedRep, event_index: int) -> dict[str, Any]:
    return {
        "event_index": event_index,
        "origin_time": iso_hundredths(rep.origin_time),
        "lat": rep.lat,
        "lon": rep.lon,
        "depth": rep.depth,
        "magnitude_mpd": rep.magnitude_mpd,
        "author": rep.author,
        "solutions": [],
    }


def append_solution(event: dict[str, Any], rep: ParsedRep) -> None:
    solutions = event.setdefault("solutions", [])
    reference_time = parse_iso(str(event["origin_time"]))
    solution = {
        "index": len(solutions) + 1,
        "dt_ref": round((rep.report_time - rep.origin_time).total_seconds(), 2),
        "type": "Mpd",
        "mpd": rep.magnitude_mpd,
        "lat": round(rep.lat, 3),
        "lon": round(rep.lon, 3),
        "depth": rep.depth,
        "origin_time": iso_hundredths(rep.origin_time),
        "station_count": rep.station_count,
        "creation_time": time_hundredths(rep.report_time),
        "author": rep.author,
        "dt_curr": round((rep.report_time - reference_time).total_seconds(), 2),
        "rep_file": rep.path.name,
    }
    solutions.append(solution)


def load_summary(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a top-level JSON array")
    return data


def atomic_write_json(path: Path, data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def known_rep_names(events: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for event in events:
        for solution in event.get("solutions", []):
            name = solution.get("rep_file")
            if isinstance(name, str):
                names.add(name)
    return names


def stable_files(inbox: Path, minimum_age_sec: float) -> list[Path]:
    now = time.time()
    result = []
    for path in sorted(inbox.glob("*.rep")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if stat.st_size > 0 and now - stat.st_mtime >= minimum_age_sec:
            result.append(path)
    return result


def archive_rep(rep: ParsedRep, archive_root: Path) -> Path:
    folder = archive_root / rep.report_time.strftime("%Y_%m")
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / rep.path.name
    if destination.exists():
        if destination.read_bytes() == rep.path.read_bytes():
            rep.path.unlink()
            return destination
        stamp = rep.report_time.strftime("%Y%m%d_%H%M%S_%f")
        destination = folder / f"{rep.path.stem}_{stamp}{rep.path.suffix}"
    shutil.move(str(rep.path), str(destination))
    return destination


def run_git(
    repo_dir: Path,
    *args: str,
    check: bool = True,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo_dir), *args]
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def verify_git_repository(repo_dir: Path) -> None:
    result = run_git(repo_dir, "rev-parse", "--show-toplevel")
    actual = Path(result.stdout.strip()).resolve()
    if actual != repo_dir.resolve():
        raise RuntimeError(f"--repo-dir must be the Git repository root: {actual}")


def load_dotenv(path: Path) -> dict[str, str]:
    """Load KEY=VALUE credentials without requiring python-dotenv."""
    if not path.is_file():
        raise RuntimeError(
            f"missing credential file: {path}\n"
            "Create it with GITHUB_USERNAME and GITHUB_TOKEN."
        )
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f"invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeError(f"invalid quote on .env line {line_number}")
            value = value[1:-1]
        values[key] = value
    return values


def protect_env_file(repo_dir: Path, env_path: Path) -> None:
    """Exclude the credential file locally and refuse to use it if tracked."""
    try:
        relative = env_path.resolve().relative_to(repo_dir.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("--env-file must be inside --repo-dir") from exc

    tracked = run_git(
        repo_dir, "ls-files", "--error-unmatch", "--", relative, check=False
    )
    if tracked.returncode == 0:
        raise RuntimeError(
            f"{relative} is already tracked. Remove it before use:\n"
            f"git rm --cached -- {relative}"
        )

    git_path = run_git(repo_dir, "rev-parse", "--git-path", "info/exclude")
    exclude_path = Path(git_path.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = repo_dir / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        exclude_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if exclude_path.exists()
        else []
    )
    if relative not in {line.strip() for line in existing}:
        with exclude_path.open("a", encoding="utf-8", newline="\n") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            handle.write(f"{relative}\n")


def git_auth_environment(repo_dir: Path, env_path: Path) -> dict[str, str]:
    """
    Supply credentials with GIT_ASKPASS.

    The token is never placed in the remote URL, command arguments, repository,
    or askpass helper file.
    """
    values = load_dotenv(env_path)
    username = values.get("GITHUB_USERNAME", "").strip()
    token = values.get("GITHUB_TOKEN", "").strip()
    if not username or not token:
        raise RuntimeError(
            f"{env_path} must define non-empty GITHUB_USERNAME and GITHUB_TOKEN"
        )

    git_path = run_git(repo_dir, "rev-parse", "--git-path", "eewrep_auth")
    helper_dir = Path(git_path.stdout.strip())
    if not helper_dir.is_absolute():
        helper_dir = repo_dir / helper_dir
    helper_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        helper = helper_dir / "git_askpass.bat"
        helper.write_text(
            "@echo off\r\n"
            'echo %~1 | findstr /I "Username" >nul\r\n'
            "if %errorlevel%==0 (\r\n"
            "  echo %EEW_GIT_USERNAME%\r\n"
            ") else (\r\n"
            "  echo %EEW_GIT_TOKEN%\r\n"
            ")\r\n",
            encoding="utf-8",
        )
    else:
        helper = helper_dir / "git_askpass.sh"
        helper.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *sername*) printf "%s\\n" "$EEW_GIT_USERNAME" ;;\n'
            '  *) printf "%s\\n" "$EEW_GIT_TOKEN" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)

    return {
        "GIT_ASKPASS": str(helper),
        "GIT_TERMINAL_PROMPT": "0",
        "EEW_GIT_USERNAME": username,
        "EEW_GIT_TOKEN": token,
    }


def sync_remote(
    repo_dir: Path,
    remote: str,
    branch: str,
    git_env: Mapping[str, str],
) -> None:
    # Pull before reading the summary so concurrent remote updates are retained.
    run_git(
        repo_dir,
        "pull",
        "--rebase",
        "--autostash",
        remote,
        branch,
        extra_env=git_env,
    )


def commit_and_push(
    repo_dir: Path,
    relative_paths: list[Path],
    remote: str,
    branch: str,
    git_env: Mapping[str, str],
) -> bool:
    if not relative_paths:
        return False
    pathspecs = [str(path.as_posix()) for path in relative_paths]
    run_git(repo_dir, "add", "--", *pathspecs)
    staged = run_git(repo_dir, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise RuntimeError(staged.stderr.strip() or "git diff --cached failed")

    count = sum(path.suffix.lower() == ".rep" for path in relative_paths)
    message = f"Append {count} eew .rep solution{'s' if count != 1 else ''}"
    run_git(repo_dir, "commit", "-m", message)
    run_git(repo_dir, "push", remote, f"HEAD:{branch}", extra_env=git_env)
    return True


def process_once(args: argparse.Namespace) -> int:
    repo_dir = args.repo_dir.resolve()
    inbox = args.inbox.resolve() if args.inbox else repo_dir
    archive_root = args.archive_root.resolve() if args.archive_root else repo_dir
    summary_path = repo_dir / args.summary

    verify_git_repository(repo_dir)
    git_env: dict[str, str] = {}
    if not args.no_git:
        env_path = repo_dir / args.env_file
        protect_env_file(repo_dir, env_path)
        git_env = git_auth_environment(repo_dir, env_path)
        sync_remote(repo_dir, args.remote, args.branch, git_env)

    events = load_summary(summary_path)
    known = known_rep_names(events)
    candidates = stable_files(inbox, args.minimum_age)
    if not candidates:
        LOG.info("No stable .rep files found in %s", inbox)
        return 0

    parsed: list[ParsedRep] = []
    archived_relative: list[Path] = []
    for path in candidates:
        if path.name in known:
            LOG.warning("Already recorded; archiving without appending: %s", path.name)
            try:
                parsed_rep = parse_rep(path)
                archived = archive_rep(parsed_rep, archive_root)
                try:
                    archived_relative.append(archived.relative_to(repo_dir))
                except ValueError as exc:
                    raise RuntimeError(
                        "archive root must be inside the repository when Git push is enabled"
                    ) from exc
            except (OSError, RepParseError) as exc:
                LOG.error("Cannot archive duplicate %s: %s", path.name, exc)
            continue
        try:
            parsed.append(parse_rep(path))
        except (OSError, RepParseError) as exc:
            # Leave an incomplete/bad file in place so a later scan can retry it.
            LOG.error("Skipping %s: %s", path.name, exc)

    if not parsed and not archived_relative:
        return 0

    # Deterministic order makes retries and JSON diffs reproducible.
    parsed.sort(key=lambda item: (item.report_time, item.path.name))
    next_event_index = max(
        (int(event.get("event_index", 0)) for event in events), default=0
    ) + 1
    for rep in parsed:
        event = find_event(
            events,
            rep,
            args.event_time_tolerance,
            args.event_distance_tolerance,
        )
        if event is None:
            event = make_event(rep, next_event_index)
            next_event_index += 1
            events.append(event)
        append_solution(event, rep)
        archived = archive_rep(rep, archive_root)
        try:
            archived_relative.append(archived.relative_to(repo_dir))
        except ValueError as exc:
            raise RuntimeError(
                "archive root must be inside the repository when Git push is enabled"
            ) from exc
        LOG.info("Appended and archived %s -> %s", rep.path.name, archived)

    if parsed:
        atomic_write_json(summary_path, events)
    changed = (
        [summary_path.relative_to(repo_dir), *archived_relative]
        if parsed
        else archived_relative
    )
    if args.no_git:
        LOG.info("Updated %s; Git actions disabled", summary_path)
    elif commit_and_push(repo_dir, changed, args.remote, args.branch, git_env):
        LOG.info("Committed and pushed %d parsed .rep files", len(parsed))
    return len(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="local EEWS_GAS_Dashboard_Data clone (default: current directory)",
    )
    parser.add_argument(
        "--inbox",
        type=Path,
        help="folder receiving new .rep files (default: repository root)",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="parent for YYYY_MM archive folders (default: repository root)",
    )
    parser.add_argument("--summary", default="eewrep_summary.json")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="credential file inside the repository (default: .env)",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--minimum-age", type=float, default=10.0)
    parser.add_argument("--event-time-tolerance", type=float, default=6.0)
    parser.add_argument("--event-distance-tolerance", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="parse/archive/update JSON without pull, commit, or push",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")

    while True:
        try:
            process_once(args)
        except KeyboardInterrupt:
            LOG.info("Stopped")
            return 0
        except Exception:
            LOG.exception("Processing cycle failed; files were left for retry")
            if args.once:
                return 1
        if args.once:
            return 0
        LOG.info("Next scan in %d seconds", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
