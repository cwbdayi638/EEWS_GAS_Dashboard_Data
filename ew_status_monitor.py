#!/usr/bin/env python3
"""
Continuously monitor Earthworm ring buffers and push status to GitHub.

Designed to run inside a local clone of:
https://github.com/cwbdayi638/EEWS_GAS_Dashboard_Data

Checks:
  * WAVE_RING_CWASN and WAVE_RING_TSMIP using sniffwave and findwave
  * PICK_RING and EEW_RING using sniffring

Default behavior:
  * collect status from all rings every 300 seconds;
  * write status to ew_status.json with timestamps;
  * optionally maintain history in ew_status_history.json;
  * commit and push to origin/main.

Google Drive 上傳（選用）：
  * 在 .env 中設定 GDRIVE_SERVICE_ACCOUNT_JSON 與 GDRIVE_FOLDER_ID，
    腳本會在每次週期結束後自動將 ew_status.json 上傳至 Google Drive；
    GitHub 程式碼庫本身不受影響。
  * 使用 --no-drive 旗標可停用 Drive 上傳。

Before running, source the Earthworm environment:
    source /opt/earthworm/ew_linux.bash

Run once for testing:
    python ew_status_monitor.py --once --repo-dir C:\\path\\to\\EEWS_GAS_Dashboard_Data

Run continuously:
    python ew_status_monitor.py --repo-dir C:\\path\\to\\EEWS_GAS_Dashboard_Data

Create .env in the repository root. GITHUB_TOKEN must be a GitHub Personal
Access Token (PAT), not the GitHub account password:
    GITHUB_USERNAME=your_github_username
    GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx

For Google Drive upload, also add to .env:
    GDRIVE_SERVICE_ACCOUNT_JSON=/path/to/service_account.json
    GDRIVE_FOLDER_ID=your_google_drive_folder_id
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# Google Drive 上傳模組（選用）
try:
    from drive_uploader import DriveUploader
    _DRIVE_UPLOADER_AVAILABLE = True
except ImportError:
    _DRIVE_UPLOADER_AVAILABLE = False


LOG = logging.getLogger("ew_status_monitor")
DEFAULT_REPOSITORY = "https://github.com/cwbdayi638/EEWS_GAS_Dashboard_Data"


@dataclass
class WaveRingStatus:
    """Status from snifwave/findwave for a wave ring."""
    ring_name: str
    check_time: str
    station_count: int
    channel_count: int
    latest_packet_time: str | None
    data_gap_seconds: float | None
    stations: list[dict[str, Any]]
    raw_output: str
    error: str | None = None


@dataclass
class PickRingStatus:
    """Status from sniffring for PICK_RING or EEW_RING."""
    ring_name: str
    check_time: str
    message_count: int
    message_types: dict[str, int]
    latest_message_time: str | None
    raw_output: str
    error: str | None = None


@dataclass
class PickFileStatus:
    """Status from reading a pick file."""
    file_path: str
    check_time: str
    line_count: int
    last_lines: list[str]
    file_size: int
    last_modified: str | None
    error: str | None = None


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4] + "Z"


def run_command(
    command: list[str],
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        environment = os.environ.copy()
        if env:
            environment.update(env)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timeout after {timeout}s"
    except Exception as exc:
        return -1, "", str(exc)


def check_wave_ring_snifwave(ring_name: str, duration: int = 10) -> WaveRingStatus:
    """Check wave ring status using sniffwave."""
    check_time = get_timestamp()
    command = ["sniffwave", ring_name, str(duration)]
    returncode, stdout, stderr = run_command(command, timeout=duration + 20)
    
    if returncode != 0:
        return WaveRingStatus(
            ring_name=ring_name,
            check_time=check_time,
            station_count=0,
            channel_count=0,
            latest_packet_time=None,
            data_gap_seconds=None,
            stations=[],
            raw_output=stdout + stderr,
            error=f"sniffwave failed: {stderr or 'non-zero exit'}",
        )
    
    # Parse snifwave output
    stations: dict[str, dict[str, Any]] = {}
    latest_time: datetime | None = None
    
    for line in stdout.splitlines():
        # Example: "TSMIP.CHY.00.HLZ 2026-08-04 03:15:23.45"
        match = re.match(
            r"^\s*(\S+)\.(\S+)\.(\S+)\.(\S+)\s+"
            r"(\d{4})-(\d{2})-(\d{2})\s+"
            r"(\d{2}):(\d{2}):(\d{2})\.(\d+)",
            line
        )
        if match:
            net, sta, loc, chan = match.group(1, 2, 3, 4)
            timestamp_str = f"{match.group(5)}-{match.group(6)}-{match.group(7)}T" \
                           f"{match.group(8)}:{match.group(9)}:{match.group(10)}.{match.group(11)}"
            
            key = f"{net}.{sta}"
            if key not in stations:
                stations[key] = {
                    "network": net,
                    "station": sta,
                    "channels": [],
                    "latest_packet": timestamp_str,
                }
            
            stations[key]["channels"].append({
                "location": loc,
                "channel": chan,
                "latest_packet": timestamp_str,
            })
            
            # Track latest time
            try:
                packet_time = datetime.fromisoformat(timestamp_str.rstrip("Z"))
                if latest_time is None or packet_time > latest_time:
                    latest_time = packet_time
            except ValueError:
                pass
    
    # Calculate data gap
    data_gap = None
    if latest_time:
        gap = (datetime.now(timezone.utc).replace(tzinfo=None) - latest_time).total_seconds()
        data_gap = round(gap, 2)
    
    station_list = list(stations.values())
    return WaveRingStatus(
        ring_name=ring_name,
        check_time=check_time,
        station_count=len(stations),
        channel_count=sum(len(s["channels"]) for s in station_list),
        latest_packet_time=latest_time.isoformat() if latest_time else None,
        data_gap_seconds=data_gap,
        stations=station_list,
        raw_output=stdout,
    )


def check_wave_ring_findwave(ring_name: str) -> WaveRingStatus:
    """Check wave ring status using findwave."""
    check_time = get_timestamp()
    command = ["findwave", ring_name]
    returncode, stdout, stderr = run_command(command)
    
    if returncode != 0:
        return WaveRingStatus(
            ring_name=ring_name,
            check_time=check_time,
            station_count=0,
            channel_count=0,
            latest_packet_time=None,
            data_gap_seconds=None,
            stations=[],
            raw_output=stdout + stderr,
            error=f"findwave failed: {stderr or 'non-zero exit'}",
        )
    
    # Parse findwave output (lists available SCNL)
    channels: dict[str, dict[str, Any]] = {}
    
    for line in stdout.splitlines():
        # Example: "TSMIP CHY 00 HLZ"
        match = re.match(r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if match:
            net, sta, loc, chan = match.groups()
            key = f"{net}.{sta}"
            if key not in channels:
                channels[key] = {
                    "network": net,
                    "station": sta,
                    "channels": [],
                }
            channels[key]["channels"].append({
                "location": loc,
                "channel": chan,
            })
    
    station_list = list(channels.values())
    return WaveRingStatus(
        ring_name=ring_name,
        check_time=check_time,
        station_count=len(channels),
        channel_count=sum(len(s["channels"]) for s in station_list),
        latest_packet_time=None,
        data_gap_seconds=None,
        stations=station_list,
        raw_output=stdout,
    )


def check_pick_ring(ring_name: str, duration: int = 10) -> PickRingStatus:
    """Check pick/event ring status using sniffring."""
    check_time = get_timestamp()
    command = ["sniffring", ring_name, str(duration)]
    returncode, stdout, stderr = run_command(command, timeout=duration + 20)
    
    if returncode != 0:
        return PickRingStatus(
            ring_name=ring_name,
            check_time=check_time,
            message_count=0,
            message_types={},
            latest_message_time=None,
            raw_output=stdout + stderr,
            error=f"sniffring failed: {stderr or 'non-zero exit'}",
        )
    
    # Parse sniffring output
    message_types: dict[str, int] = {}
    message_count = 0
    latest_time: datetime | None = None
    
    for line in stdout.splitlines():
        # Count message types (example: "TYPE_PICK2K" or similar)
        type_match = re.search(r"TYPE_(\w+)", line)
        if type_match:
            msg_type = type_match.group(1)
            message_types[msg_type] = message_types.get(msg_type, 0) + 1
            message_count += 1
        
        # Try to extract timestamps
        time_match = re.search(
            r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)",
            line
        )
        if time_match:
            try:
                timestamp_str = f"{time_match.group(1)}-{time_match.group(2)}-{time_match.group(3)}T" \
                               f"{time_match.group(4)}:{time_match.group(5)}:{time_match.group(6)}.{time_match.group(7)}"
                msg_time = datetime.fromisoformat(timestamp_str)
                if latest_time is None or msg_time > latest_time:
                    latest_time = msg_time
            except ValueError:
                pass
    
    return PickRingStatus(
        ring_name=ring_name,
        check_time=check_time,
        message_count=message_count,
        message_types=message_types,
        latest_message_time=latest_time.isoformat() if latest_time else None,
        raw_output=stdout,
    )


def check_pick_file(file_path: str, tail_lines: int = 50) -> PickFileStatus:
    """Check pick file status by reading last N lines."""
    check_time = get_timestamp()
    path = Path(file_path)
    
    if not path.exists():
        return PickFileStatus(
            file_path=file_path,
            check_time=check_time,
            line_count=0,
            last_lines=[],
            file_size=0,
            last_modified=None,
            error=f"File not found: {file_path}",
        )
    
    try:
        # Get file stats
        stat = path.stat()
        file_size = stat.st_size
        last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        
        # Read last N lines
        with path.open('r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
            last_lines = [line.rstrip('\n\r') for line in lines[-tail_lines:]]
            line_count = len(lines)
        
        return PickFileStatus(
            file_path=file_path,
            check_time=check_time,
            line_count=line_count,
            last_lines=last_lines,
            file_size=file_size,
            last_modified=last_modified,
        )
    except Exception as exc:
        return PickFileStatus(
            file_path=file_path,
            check_time=check_time,
            line_count=0,
            last_lines=[],
            file_size=0,
            last_modified=None,
            error=f"Error reading file: {exc}",
        )


def collect_all_status(args: argparse.Namespace) -> dict[str, Any]:
    """Collect status from all configured rings."""
    status = {
        "timestamp": get_timestamp(),
        "wave_rings": {},
        "pick_rings": {},
        "pick_files": {},
    }
    
    # Check wave rings with sniffwave
    LOG.info("Checking WAVE_RING_CWASN with sniffwave...")
    wave_cwasn = check_wave_ring_snifwave("WAVE_RING_CWASN", args.sniff_duration)
    status["wave_rings"]["WAVE_RING_CWASN"] = asdict(wave_cwasn)
    
    LOG.info("Checking WAVE_RING_TSMIP with sniffwave...")
    wave_tsmip = check_wave_ring_snifwave("WAVE_RING_TSMIP", args.sniff_duration)
    status["wave_rings"]["WAVE_RING_TSMIP"] = asdict(wave_tsmip)
    
    # Check pick file instead of PICK_RING
    if args.pick_file:
        LOG.info("Checking pick file: %s...", args.pick_file)
        pick_file = check_pick_file(args.pick_file, args.pick_file_lines)
        status["pick_files"]["pfile_P"] = asdict(pick_file)
    
    # Check EEW ring
    LOG.info("Checking EEW_RING with sniffring...")
    eew_ring = check_pick_ring("EEW_RING", args.sniff_duration)
    status["pick_rings"]["EEW_RING"] = asdict(eew_ring)
    
    return status


def load_status_history(path: Path, max_entries: int) -> list[dict[str, Any]]:
    """Load existing status history."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        LOG.warning("Invalid JSON in %s: %s", path, exc)
        return []
    
    if not isinstance(data, list):
        LOG.warning("%s must contain a JSON array", path)
        return []
    
    # Keep only the most recent entries
    return data[-max_entries:] if len(data) > max_entries else data


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write JSON to file."""
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


def run_git(
    repo_dir: Path,
    *args: str,
    check: bool = True,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
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


def verify_git_repository(repo_dir: Path) -> bool:
    """Verify the directory is a git repository root. Returns True if valid, False otherwise."""
    try:
        result = run_git(repo_dir, "rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0:
            return False
        actual = Path(result.stdout.strip()).resolve()
        return actual == repo_dir.resolve()
    except Exception:
        return False


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
    """Supply credentials with GIT_ASKPASS."""
    values = load_dotenv(env_path)
    username = values.get("GITHUB_USERNAME", "").strip()
    token = values.get("GITHUB_TOKEN", "").strip()
    if not username or not token:
        raise RuntimeError(
            f"{env_path} must define non-empty GITHUB_USERNAME and GITHUB_TOKEN"
        )

    git_path = run_git(repo_dir, "rev-parse", "--git-path", "ew_status_auth")
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
            "  echo %EW_GIT_USERNAME%\r\n"
            ") else (\r\n"
            "  echo %EW_GIT_TOKEN%\r\n"
            ")\r\n",
            encoding="utf-8",
        )
    else:
        helper = helper_dir / "git_askpass.sh"
        helper.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *sername*) printf "%s\\n" "$EW_GIT_USERNAME" ;;\n'
            '  *) printf "%s\\n" "$EW_GIT_TOKEN" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)

    return {
        "GIT_ASKPASS": str(helper),
        "GIT_TERMINAL_PROMPT": "0",
        "EW_GIT_USERNAME": username,
        "EW_GIT_TOKEN": token,
    }


def sync_remote(
    repo_dir: Path,
    remote: str,
    branch: str,
    git_env: Mapping[str, str],
) -> None:
    """Pull latest changes from remote."""
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
    relative_path: Path,
    remote: str,
    branch: str,
    git_env: Mapping[str, str],
) -> bool:
    """Commit and push status update."""
    pathspec = str(relative_path.as_posix())
    run_git(repo_dir, "add", "--", pathspec)
    staged = run_git(repo_dir, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        LOG.info("No changes to commit")
        return False
    if staged.returncode != 1:
        raise RuntimeError(staged.stderr.strip() or "git diff --cached failed")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"Update Earthworm status - {timestamp}"
    run_git(repo_dir, "commit", "-m", message)
    run_git(repo_dir, "push", remote, f"HEAD:{branch}", extra_env=git_env)
    return True


def process_once(args: argparse.Namespace) -> bool:
    """Run one monitoring cycle."""
    repo_dir = args.repo_dir.resolve()
    status_path = repo_dir / args.status_file
    history_path = repo_dir / args.history_file

    git_env: dict[str, str] = {}
    if not args.no_git:
        # Check if we're in a git repository
        if not verify_git_repository(repo_dir):
            LOG.warning(
                "%s is not a Git repository. Running without Git operations. "
                "Use --repo-dir to specify a Git repository or --no-git to suppress this warning.",
                repo_dir
            )
            args.no_git = True  # Automatically disable Git
        else:
            env_path = repo_dir / args.env_file
            protect_env_file(repo_dir, env_path)
            git_env = git_auth_environment(repo_dir, env_path)
            sync_remote(repo_dir, args.remote, args.branch, git_env)

    # Collect current status
    LOG.info("Collecting Earthworm status...")
    current_status = collect_all_status(args)
    
    # Write current status
    atomic_write_json(status_path, current_status)
    LOG.info("Updated %s", status_path)
    
    # Update history
    if args.keep_history:
        history = load_status_history(history_path, args.max_history_entries)
        history.append(current_status)
        atomic_write_json(history_path, history)
        LOG.info("Updated %s (%d entries)", history_path, len(history))

    # Commit and push
    if args.no_git:
        LOG.info("Git actions disabled")
    else:
        files_to_commit = [status_path.relative_to(repo_dir)]
        if args.keep_history:
            files_to_commit.append(history_path.relative_to(repo_dir))

        for file_path in files_to_commit:
            if commit_and_push(repo_dir, file_path, args.remote, args.branch, git_env):
                LOG.info("Committed and pushed %s", file_path)

    # ── Google Drive 上傳（選用）──────────────────────────────────────────
    if not getattr(args, "no_drive", False):
        _drive_upload_ew_status(
            args=args,
            repo_dir=repo_dir,
            status_path=status_path,
            history_path=history_path if args.keep_history else None,
        )

    return True


def _drive_upload_ew_status(
    args: argparse.Namespace,
    repo_dir: Path,
    status_path: Path,
    history_path: Path | None,
) -> None:
    """將 ew_status.json（及 history）上傳至 Google Drive。"""
    if not _DRIVE_UPLOADER_AVAILABLE:
        LOG.debug("drive_uploader 模組不存在，跳過 Drive 上傳。")
        return

    env_path = repo_dir / getattr(args, "env_file", ".env")
    try:
        uploader = DriveUploader.from_env(env_path)
    except RuntimeError as exc:
        LOG.warning("Drive 上傳跳過（設定不完整）：%s", exc)
        return
    except Exception as exc:
        LOG.error("Drive 上傳初始化失敗：%s", exc)
        return

    for json_path in [status_path, history_path]:
        if json_path is None or not json_path.exists():
            continue
        try:
            uploader.upload_json_string(
                json_path.name,
                json_path.read_text(encoding="utf-8"),
            )
        except Exception as exc:
            LOG.error("Drive 上傳 %s 失敗：%s", json_path.name, exc)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="local repository clone (default: current directory)",
    )
    parser.add_argument(
        "--status-file",
        default="ew_status.json",
        help="current status JSON file (default: ew_status.json)",
    )
    parser.add_argument(
        "--history-file",
        default="ew_status_history.json",
        help="status history JSON file (default: ew_status_history.json)",
    )
    parser.add_argument(
        "--keep-history",
        action="store_true",
        help="maintain history of status checks",
    )
    parser.add_argument(
        "--max-history-entries",
        type=int,
        default=288,
        help="max history entries to keep (default: 288 = 24 hours at 5 min intervals)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="credential file inside the repository (default: .env)",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="git remote name (default: origin)",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="git branch name (default: main)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="monitoring interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--sniff-duration",
        type=int,
        default=10,
        help="duration for snifwave/sniffring in seconds (default: 10)",
    )
    parser.add_argument(
        "--pick-file",
        type=str,
        default="/opt/earthworm/run_working/params/pfile_P.p12",
        help="pick file to monitor (default: /opt/earthworm/run_working/params/pfile_P.p12)",
    )
    parser.add_argument(
        "--pick-file-lines",
        type=int,
        default=50,
        help="number of lines to read from pick file (default: 50)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run once and exit (for testing)",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="update JSON files without pull, commit, or push",
    )
    parser.add_argument(
        "--no-drive",
        action="store_true",
        help="disable Google Drive upload even if .env contains Drive credentials",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser


def main() -> int:
    """Main entry point."""
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    
    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    if args.sniff_duration < 1:
        raise SystemExit("--sniff-duration must be at least 1 second")

    while True:
        try:
            process_once(args)
        except KeyboardInterrupt:
            LOG.info("Stopped by user")
            return 0
        except Exception:
            LOG.exception("Processing cycle failed")
            if args.once:
                return 1
        
        if args.once:
            return 0
        
        LOG.info("Next check in %d seconds", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
