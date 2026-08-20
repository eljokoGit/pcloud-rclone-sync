"""Triggering of scheduled synchronisations.

A thread checks every 20 seconds whether a profile should start. A profile
fires at most once per day and per time slot, even if the application
restarts in between: consumed slots are recorded on disk.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from .config import Config
from .engine import SyncEngine
from .store import ProfileStore

log = logging.getLogger("pcloud-sync.scheduler")


class Scheduler:
    def __init__(self, config: Config, engine: SyncEngine, store: ProfileStore) -> None:
        self.config = config
        self.engine = engine
        self.store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # profile -> "YYYY-MM-DD HH:MM". On disk: a restart within the
        # scheduled minute would otherwise fire the same backup again.
        self._fired_file = config.profiles_file.parent / "scheduler.json"
        self._fired: dict[str, str] = self._load_fired()

    def _load_fired(self) -> dict[str, str]:
        try:
            raw = json.loads(self._fired_file.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_fired(self) -> None:
        try:
            tmp = self._fired_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._fired), encoding="utf-8")
            tmp.replace(self._fired_file)
        except OSError:
            pass

    def start(self) -> None:
        # The thread runs in every case: scheduled profiles can be created
        # from the interface after startup.
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Scheduler started.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def next_run(self, profile_id: str) -> str | None:
        profile = self.store.get(profile_id)
        if not profile or not profile.scheduled:
            return None
        hour, minute = (int(x) for x in profile.schedule.split(":"))
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat(timespec="minutes")

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            current = now.strftime("%H:%M")

            for profile in self.store.profiles:
                if not profile.scheduled or profile.schedule != current:
                    continue
                if self._fired.get(profile.id) == stamp:
                    continue

                log.info("%s: scheduled start.", profile.id)
                try:
                    self.engine.full_cycle(profile.id, trigger="scheduled")
                except Exception as exc:  # noqa: BLE001
                    # The launch may be refused because the engine runs one
                    # operation at a time. The slot is not marked consumed:
                    # retry on the next pass, as long as the scheduled
                    # minute has not elapsed.
                    log.warning("%s: %s", profile.id, exc)
                else:
                    self._fired[profile.id] = stamp
                    self._save_fired()

            self._stop.wait(20)
