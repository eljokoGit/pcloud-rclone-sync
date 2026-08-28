"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_SCHEDULE_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    """Invalid configuration. The message is meant for the user."""


def default_data_dir() -> Path:
    """Default data folder, derived at runtime.

    Hardcoding "C:/ProgramData" in a shipped file would break any machine
    where it lives elsewhere, and would not be portable at all outside
    Windows. Explicit paths in config.yaml still take precedence.
    """
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "PCloudSync"
    return Path.home() / ".local" / "share" / "pcloud-sync"


def ensure_config(path: Path | str) -> Path:
    """Creates config.yaml from config.example.yaml on first launch.

    The tracked file is the example: the real config.yaml belongs to the
    user (comments included) and updating the application must never
    overwrite it.
    """
    path = Path(path)
    if path.exists():
        return path
    example = path.with_name("config.example.yaml")
    if example.exists():
        try:
            shutil.copyfile(example, path)
        except OSError:
            return path
    return path


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8477
    open_browser: bool = True

    rclone_binary: str = "rclone"
    rc_port: int = 5577
    log_dir: Path = Path("logs")
    transfers: int = 8
    checkers: int = 16
    retries: int = 5
    low_level_retries: int = 20
    bandwidth_limit: str = ""
    # Server-side repositioning of moved files. Off by default: it changes
    # the remote tree behind the pCloud client's back, and a client watching
    # the same folders repairs the divergence it sees with deletions and
    # downloads. Only worth enabling when no pCloud client watches the
    # backed-up folders.
    track_renames: bool = False

    database: Path = Path("history.db")
    profiles_file: Path = Path("profiles.json")
    exclude: list[str] = field(default_factory=list)
    skip_hidden: bool = False
    update_check: bool = True
    drift_check_hours: float = 24.0
    seed_profiles: list[dict] = field(default_factory=list)


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} contains a YAML error:\n{exc}") from exc

    server = raw.get("server") or {}
    rclone = raw.get("rclone") or {}
    data_dir = default_data_dir()

    cfg = Config(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8477)),
        open_browser=bool(server.get("open_browser", True)),
        rclone_binary=rclone.get("binary", "rclone"),
        rc_port=int(rclone.get("rc_port", 5577)),
        log_dir=Path(rclone.get("log_dir") or data_dir / "logs"),
        transfers=int(rclone.get("transfers", 8)),
        checkers=int(rclone.get("checkers", 16)),
        retries=int(rclone.get("retries", 5)),
        low_level_retries=int(rclone.get("low_level_retries", 20)),
        bandwidth_limit=rclone.get("bandwidth_limit", "") or "",
        track_renames=bool(rclone.get("track_renames", False)),
        database=Path(raw.get("database") or data_dir / "history.db"),
        profiles_file=Path(
            raw.get("profiles_file")
            or Path(raw.get("database") or data_dir / "history.db").parent / "profiles.json"
        ),
        exclude=list(raw.get("exclude") or []),
        skip_hidden=bool(raw.get("skip_hidden", False)),
        update_check=bool(raw.get("update_check", True)),
        drift_check_hours=float(raw.get("drift_check_hours", 24)),
    )

    # Profiles live in profiles.json, managed from the interface. Any left
    # here are imported once, on first startup.
    entries = raw.get("profiles") or []

    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        for key in ("id", "name", "local", "remote"):
            if not entry.get(key):
                raise ConfigError(f"Profile #{index}: the '{key}' field is required.")

        pid = str(entry["id"]).strip()
        if " " in pid:
            raise ConfigError(f"Profile '{pid}': the id must not contain spaces.")
        if pid in seen:
            raise ConfigError(f"The id '{pid}' is used by two profiles.")
        seen.add(pid)

        schedule = str(entry.get("schedule") or "").strip()
        if schedule and not _SCHEDULE_RE.match(schedule):
            raise ConfigError(
                f"Profile '{pid}': invalid schedule '{schedule}'. Expected format: HH:MM."
            )

        remote = str(entry["remote"]).strip()
        if ":" not in remote:
            raise ConfigError(
                f"Profile '{pid}': the destination must include an rclone remote, "
                f"for example 'pcloud:some/path'."
            )

        cfg.seed_profiles.append(
            dict(
                id=pid,
                name=str(entry["name"]).strip(),
                local=str(entry["local"]).strip(),
                remote=remote,
                schedule=schedule,
                auto=bool(entry.get("auto", False)),
                max_deletes=int(entry.get("max_deletes", 500)),
            )
        )

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.database.parent.mkdir(parents=True, exist_ok=True)
    cfg.profiles_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg
