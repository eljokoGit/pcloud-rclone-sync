"""Backup profiles, created and edited from the interface.

Profiles live in their own JSON file, not in config.yaml: the latter stays a
file the user writes by hand, with their own comments, and that the
application never rewrites.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path

_SCHEDULE_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ProfileError(Exception):
    """Invalid profile. The message is meant for the user."""


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return base[:40] or uuid.uuid4().hex[:8]


@dataclass
class Profile:
    id: str
    name: str
    local: str
    remote: str
    schedule: str = ""
    auto: bool = False
    max_deletes: int = 500
    enabled: bool = True

    @property
    def scheduled(self) -> bool:
        return bool(self.schedule) and self.enabled

    def to_dict(self) -> dict:
        return asdict(self)


def validate(data: dict, existing_ids: set[str], current_id: str | None = None) -> dict:
    """Cleans and checks profile fields. Raises ProfileError when invalid."""
    name = str(data.get("name") or "").strip()
    local = str(data.get("local") or "").strip()
    remote = str(data.get("remote") or "").strip()

    if not name:
        raise ProfileError("Give this profile a name.")
    if not local:
        raise ProfileError("Choose a source folder or drive.")
    if not remote:
        raise ProfileError("Choose a destination on pCloud.")
    if ":" not in remote:
        raise ProfileError(
            "The destination must start with an rclone remote, for example “pcloud:”."
        )

    schedule = str(data.get("schedule") or "").strip()
    if schedule and not _SCHEDULE_RE.match(schedule):
        raise ProfileError(f"Invalid schedule “{schedule}”. Expected format: HH:MM.")

    try:
        max_deletes = int(data.get("max_deletes", 500))
    except (TypeError, ValueError):
        raise ProfileError("The deletion threshold must be a number.")

    pid = str(data.get("id") or "").strip() or _slug(name)
    if pid != current_id:
        candidate, n = pid, 2
        while candidate in existing_ids:
            candidate = f"{pid}-{n}"
            n += 1
        pid = candidate

    return {
        "id": pid,
        "name": name,
        "local": local,
        "remote": remote.rstrip("/"),
        "schedule": schedule,
        "auto": bool(data.get("auto", False)),
        "max_deletes": max_deletes,
        "enabled": bool(data.get("enabled", True)),
    }


class ProfileStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._profiles: list[Profile] = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Profile)}
            # Unknown keys (future version, manual edit) are ignored; a
            # missing required field raises TypeError and gets the same
            # treatment as unreadable JSON: the application must start.
            self._profiles = [
                Profile(**{k: v for k, v in entry.items() if k in known})
                for entry in raw.get("profiles", [])
            ]
        except (json.JSONDecodeError, OSError, TypeError, AttributeError):
            self._profiles = []
            backup = self.path.with_suffix(".json.corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass

    def _save(self) -> None:
        payload = {"profiles": [p.to_dict() for p in self._profiles]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- reading ---------------------------------------------------------------

    @property
    def profiles(self) -> list[Profile]:
        with self._lock:
            return list(self._profiles)

    def get(self, profile_id: str) -> Profile | None:
        with self._lock:
            return next((p for p in self._profiles if p.id == profile_id), None)

    def ids(self) -> set[str]:
        with self._lock:
            return {p.id for p in self._profiles}

    # -- writing ---------------------------------------------------------------

    def add(self, data: dict) -> Profile:
        with self._lock:
            clean = validate(data, self.ids())
            profile = Profile(**clean)
            self._profiles.append(profile)
            self._save()
            return profile

    def add_many(self, entries: list[dict]) -> list[Profile]:
        """Creates several profiles at once. All or nothing."""
        with self._lock:
            snapshot = list(self._profiles)
            created: list[Profile] = []
            try:
                for entry in entries:
                    clean = validate(entry, self.ids())
                    profile = Profile(**clean)
                    self._profiles.append(profile)
                    created.append(profile)
                self._save()
                return created
            except ProfileError:
                self._profiles = snapshot
                raise

    def update(self, profile_id: str, data: dict) -> Profile:
        with self._lock:
            current = self.get(profile_id)
            if current is None:
                raise ProfileError("This profile no longer exists.")
            merged = {**current.to_dict(), **data, "id": profile_id}
            clean = validate(merged, self.ids(), current_id=profile_id)
            updated = Profile(**clean)
            self._profiles = [updated if p.id == profile_id else p for p in self._profiles]
            self._save()
            return updated

    def remove(self, profile_id: str) -> None:
        with self._lock:
            before = len(self._profiles)
            self._profiles = [p for p in self._profiles if p.id != profile_id]
            if len(self._profiles) == before:
                raise ProfileError("This profile no longer exists.")
            self._save()

    def seed(self, entries: list[dict]) -> int:
        """Initial import from config.yaml, only when the store is empty."""
        with self._lock:
            if self._profiles or not entries:
                return 0
            for entry in entries:
                try:
                    self._profiles.append(Profile(**validate(entry, self.ids())))
                except ProfileError:
                    continue
            self._save()
            return len(self._profiles)
