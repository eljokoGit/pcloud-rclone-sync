"""Synchronisation orchestration.

Every profile follows the same cycle:

    idle  ->  analysis  ->  [validation]  ->  transfer  ->  idle

The analysis is a simulation: it modifies nothing and produces the summary
(server-side moves, files to upload, deletions). Moving on to the transfer is
automatic when the profile allows it and the number of deletions stays under
the configured threshold; otherwise the run stops and waits for the user's
validation.
"""

from __future__ import annotations

import logging
import os
import re
import json
import stat as stat_module
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import History
from .store import Profile, ProfileStore
from .rclone import RcloneEngine, RcloneError

log = logging.getLogger("pcloud-sync.engine")

# Lines rclone emits during a dry run
_RE_DELETE = re.compile(r"NOTICE: (.+?): Skipped delete as --dry-run is set")
_RE_COPY = re.compile(
    r"NOTICE: (.+?): Skipped copy as --dry-run is set(?: \(size ([\d.]+)\s?([KMGT]?)i?B?\))?"
)
# Depending on the rclone version the line reads "Skipped move as ..." or
# "Skipped move to <target> as ...". Both are accepted.
_RE_MOVE_SIZE = re.compile(
    r"Skipped move(?: to .+?)? as --dry-run is set \(size ([\d.]+)\s?([KMGT]?)i?B?\)"
)

_UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

# System folders, excluded from the count as from the synchronisation
_IGNORED_DIRS = {"$recycle.bin", "system volume information", "config.msi"}

# Characters rclone's glob patterns treat as syntax
_GLOB_SPECIALS = set("*?[]{}\\")


def _glob_escape(name: str) -> str:
    """Escapes rclone filter glob characters in a literal file name."""
    return "".join("\\" + c if c in _GLOB_SPECIALS else c for c in name)


def hidden_excludes(root: str, cap: int = 1000) -> list[str]:
    """Builds rclone exclude rules for hidden files and folders.

    rclone has no attribute-based filter: the Windows hidden bit is
    invisible to its glob rules. The pCloud client silently skips hidden
    items, and a backup meant to plug into one must do the same — observed:
    a hidden folder of 183 GB that the client had never uploaded.

    Dot-names count as hidden on every platform (the pCloud client's
    default exclusions cover them too), but they need no scan: two generic
    rules match them all. They MUST NOT become one rule per file — a drive
    full of dot-files once flooded the rule cap and silently dropped the
    exclusion of that 183 GB attribute-hidden folder (observed 2026-08-21).
    The walk only collects attribute-hidden items, which number in the
    dozens, and prunes dot-folders instead of descending into them.
    """
    rules: list[str] = [".*", ".*/**"]
    if os.name != "nt":
        return rules

    scanned = 0
    stack = [(root, "")]
    while stack and scanned < cap:
        current, prefix = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if scanned >= cap:
                break
            if entry.name.startswith("."):
                continue  # covered by the two generic rules
            try:
                attrs = entry.stat(follow_symlinks=False).st_file_attributes
                hidden = bool(attrs & stat_module.FILE_ATTRIBUTE_HIDDEN)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if hidden:
                    rules.append(f"/{prefix}{_glob_escape(entry.name)}/**")
                    scanned += 1
                else:
                    stack.append((entry.path, f"{prefix}{_glob_escape(entry.name)}/"))
            elif hidden:
                rules.append(f"/{prefix}{_glob_escape(entry.name)}")
                scanned += 1
    if scanned >= cap:
        log.warning(
            "Hidden-item scan reached the %d-rule cap: items beyond it will sync.", cap
        )
    return rules

class Cancelled(Exception):
    """The user interrupted the operation. Distinct from a failure."""


IDLE = "idle"
ANALYSIS = "analysis"
VALIDATION = "validation"
TRANSFER = "transfer"
ERROR = "error"


@dataclass
class Plan:
    """Summary produced by an analysis."""

    moved: int = 0
    transfers: int = 0
    deletes: int = 0
    checks: int = 0
    bytes_to_send: int = 0
    bytes_moved: int = 0
    delete_samples: list[str] = field(default_factory=list)
    delete_by_folder: list[dict] = field(default_factory=list)
    send_samples: list[dict] = field(default_factory=list)
    send_by_folder: list[dict] = field(default_factory=list)
    computed_at: str = ""

    @property
    def empty(self) -> bool:
        return self.moved == 0 and self.transfers == 0 and self.deletes == 0

    def to_dict(self, full: bool = False) -> dict:
        """View of the plan.

        Lists are truncated by default: the current state is polled every
        second and carrying thousands of paths each time would be pointless
        weight. The full view is served on demand, when the detail drawer
        opens.
        """
        preview = None if full else 60
        return {
            "moved": self.moved,
            "transfers": self.transfers,
            "deletes": self.deletes,
            "checks": self.checks,
            "bytes_to_send": self.bytes_to_send,
            "bytes_moved": self.bytes_moved,
            "delete_samples": self.delete_samples[:preview] if preview else self.delete_samples,
            "delete_by_folder": self.delete_by_folder,
            "send_samples": self.send_samples[:preview] if preview else self.send_samples,
            "send_by_folder": self.send_by_folder,
            "truncated": bool(preview) and (
                len(self.delete_samples) > preview or len(self.send_samples) > preview
            ),
            "computed_at": self.computed_at,
            "empty": self.empty,
        }


    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            moved=int(data.get("moved", 0)),
            transfers=int(data.get("transfers", 0)),
            deletes=int(data.get("deletes", 0)),
            checks=int(data.get("checks", 0)),
            bytes_to_send=int(data.get("bytes_to_send", 0)),
            bytes_moved=int(data.get("bytes_moved", 0)),
            delete_samples=list(data.get("delete_samples") or []),
            delete_by_folder=list(data.get("delete_by_folder") or []),
            send_samples=list(data.get("send_samples") or []),
            send_by_folder=list(data.get("send_by_folder") or []),
            computed_at=data.get("computed_at", ""),
        )


@dataclass
class State:
    """Current state of a profile, serialised as-is to the interface."""

    phase: str = IDLE
    message: str = ""
    job_id: int | None = None
    run_id: int | None = None
    started_at: float | None = None
    plan: Plan | None = None
    live: dict = field(default_factory=dict)
    last_error: str = ""
    blocked_reason: str = ""
    cancelled: bool = False

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "message": self.message,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "plan": self.plan.to_dict() if self.plan else None,
            "live": self.live,
            "last_error": self.last_error,
            "blocked_reason": self.blocked_reason,
        }


class LocalCounter:
    """Counts the files of the local tree, in the background.

    rclone leaves totalChecks at zero until its inventory is complete, which
    can take minutes on a whole drive. Without a denominator, no percentage
    can be shown. So it is computed on our side: a metadata walk, without
    reading file contents, much faster than the hashing running in parallel.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self.total = 0
        self.done = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._walk, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _walk(self) -> None:
        counted = 0
        try:
            for _, dirs, files in os.walk(self.root, onerror=lambda e: None):
                if self._stop.is_set():
                    return
                # Approximation of the synchronisation's exclusions: system
                # folders are skipped, but the file patterns from config.yaml
                # (Thumbs.db, *.tmp...) are not applied. The error on the
                # denominator stays marginal, and the percentage is capped at
                # 99 while the operation runs anyway.
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith("$") and d.lower() not in _IGNORED_DIRS
                ]
                counted += len(files)
                self.total = counted
        except OSError:
            pass
        finally:
            self.done = True


class LogTail:
    """Reads the new lines of a log file from a given position."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.position = path.stat().st_size if path.exists() else 0

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.position:  # rotation
            self.position = 0
        if size == self.position:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.position)
            blob = handle.read()
        # Only complete lines are consumed: a line still being written would
        # otherwise be split into two fragments, neither of which matches the
        # expected patterns — a lost deletion skews the plan and the
        # max_deletes safeguard.
        cut = blob.rfind(b"\n")
        if cut < 0:
            return []
        self.position += cut + 1
        return blob[:cut].decode("utf-8", errors="replace").splitlines()


class SyncEngine:
    def __init__(
        self,
        config: Config,
        rclone: RcloneEngine,
        history: History,
        store: ProfileStore,
    ) -> None:
        self.config = config
        self.rclone = rclone
        self.history = history
        self.store = store
        self._states: dict[str, State] = {}
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        # An analysis can take half an hour. Keeping it in memory only means
        # losing it on every application restart, forcing the whole thing to
        # be redone before an already-computed transfer can start.
        self._plans_dir = config.profiles_file.parent / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        # path -> (timestamp, exists). Path.exists() on a dead network drive
        # can block for seconds, and snapshot() runs on every UI poll.
        self._exists_cache: dict[str, tuple[float, bool]] = {}
        self._restore_plans()

    # -- plan persistence -------------------------------------------------

    def _plan_file(self, profile_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_id)
        return self._plans_dir / f"{safe}.json"

    def _save_plan(self, profile_id: str, plan: Plan) -> None:
        target = self._plan_file(profile_id)
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(plan.to_dict(full=True), ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError:
            pass

    def _drop_plan(self, profile_id: str) -> None:
        try:
            self._plan_file(profile_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _restore_plans(self) -> None:
        """Puts unconsumed analyses back into the validation phase."""
        # Sweep files that belong to no profile: a profiles.json replaced or
        # repaired outside the application would otherwise leave orphaned
        # plans behind forever.
        known = {self._plan_file(p.id).name for p in self.store.profiles}
        for stray in self._plans_dir.glob("*.json"):
            if stray.name not in known:
                try:
                    stray.unlink()
                except OSError:
                    pass
        for profile in self.store.profiles:
            path = self._plan_file(profile.id)
            if not path.exists():
                continue
            try:
                plan = Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                self._drop_plan(profile.id)
                continue
            if plan.empty:
                self._drop_plan(profile.id)
                continue
            state = State(
                phase=VALIDATION,
                plan=plan,
                message="Analysis recovered. Review the plan before starting the transfer.",
            )
            self._states[profile.id] = state

    # -- state reading -----------------------------------------------------

    def state(self, profile_id: str) -> State:
        with self._lock:
            return self._states.setdefault(profile_id, State())

    def busy(self, profile_id: str) -> bool:
        return self.state(profile_id).phase in (ANALYSIS, TRANSFER)

    def snapshot(self) -> dict:
        out = {}
        for profile in self.store.profiles:
            state = self.state(profile.id)
            last = self.history.last_success(profile.id)
            out[profile.id] = {
                **profile.to_dict(),
                "state": state.to_dict(),
                "last_success": last,
                "local_exists": self._local_exists(profile.local),
            }
        return out

    def _local_exists(self, path: str) -> bool:
        """Path.exists() with a short cache. Preflight still checks live."""
        now = time.time()
        cached = self._exists_cache.get(path)
        if cached is not None and now - cached[0] < 10:
            return cached[1]
        exists = Path(path).exists()
        self._exists_cache[path] = (now, exists)
        return exists

    # -- commands ----------------------------------------------------------

    def analyse(self, profile_id: str, trigger: str = "manual") -> None:
        self._launch(profile_id, self._run_analysis, trigger, ANALYSIS)

    def synchronise(self, profile_id: str, trigger: str = "manual") -> None:
        """Starts the synchronisation from the already-computed plan."""
        # The plan check and the launch happen under the same lock: two
        # simultaneous requests would otherwise both pass the check before
        # either thread has set the phase, and two synchronisations of the
        # same profile would run in parallel.
        with self._lock:
            state = self.state(profile_id)
            if state.phase != VALIDATION or state.plan is None:
                raise RuntimeError("No analysis is awaiting validation for this profile.")
            plan = state.plan
            self._launch(profile_id, lambda p, t: self._run_transfer(p, t, plan), trigger, TRANSFER)

    def full_cycle(self, profile_id: str, trigger: str = "manual") -> None:
        """Analysis then, when the conditions are met, synchronisation."""
        self._launch(profile_id, self._run_cycle, trigger, ANALYSIS)

    def cancel(self, profile_id: str) -> None:
        if self.store.get(profile_id) is None:
            raise RuntimeError("Unknown profile.")
        state = self.state(profile_id)
        if state.phase in (ANALYSIS, TRANSFER):
            # The flag is set even when the job does not exist yet: during
            # the launch window (preflight, history row insert), a cancel
            # used to be silently lost and the operation carried on. It is
            # set before the stop because without it, nothing would tell a
            # voluntary stop from a normal completion — a plan built from a
            # truncated analysis would pass for a complete summary.
            state.cancelled = True
            state.message = "Stopping."
            if state.job_id is not None:
                self.rclone.job_stop(state.job_id)
        elif state.phase == VALIDATION:
            self._drop_plan(profile_id)
            self._reset(profile_id, "Plan discarded.")

    def discard_plan(self, profile_id: str) -> None:
        """Forgets a plan whose source or destination has changed.

        The plan describes the old configuration: keeping it in validation
        would let a click meant for something else start a never-analysed
        transfer against the new destination — deletions included.
        """
        with self._lock:
            state = self._states.get(profile_id)
            if state is not None and state.phase == VALIDATION:
                self._drop_plan(profile_id)
                self._states[profile_id] = State(
                    message="The source or destination changed. "
                            "Run a new analysis before any transfer."
                )

    def forget(self, profile_id: str) -> None:
        """Forgets the state of a deleted profile."""
        with self._lock:
            self._states.pop(profile_id, None)
        self._threads.pop(profile_id, None)
        self._drop_plan(profile_id)

    def _reset(self, profile_id: str, message: str = "") -> None:
        with self._lock:
            self._states[profile_id] = State(message=message)

    def _busy_profile(self) -> str | None:
        """Name of the profile whose operation holds the engine, else None."""
        for pid, st in self._states.items():
            if st.phase in (ANALYSIS, TRANSFER):
                profile = self.store.get(pid)
                return profile.name if profile else pid
        return None

    def _launch(self, profile_id: str, target, trigger: str, phase: str) -> None:
        with self._lock:
            profile = self.store.get(profile_id)
            if profile is None:
                raise RuntimeError(f"Unknown profile: {profile_id}")
            if self.busy(profile_id):
                raise RuntimeError("An operation is already running on this profile.")
            # One operation at a time, across all profiles: the engine log is
            # shared and its lines carry no job id. Two simultaneous analyses
            # would read each other's deletions and copies.
            holder = self._busy_profile()
            if holder is not None:
                raise RuntimeError(
                    f"An operation is already running on “{holder}”. "
                    f"The engine executes one at a time."
                )
            state = self.state(profile_id)
            # The phase is reserved before the lock is released: it is what
            # makes busy() true — two simultaneous launches would otherwise
            # both pass the checks above.
            state.phase = phase
            state.cancelled = False
            state.message = ""

        thread = threading.Thread(
            target=self._guard, args=(profile, target, trigger), daemon=True
        )
        self._threads[profile_id] = thread
        thread.start()

    def _guard(self, profile: Profile, target, trigger: str) -> None:
        try:
            target(profile, trigger)
        except Cancelled:
            self._cancelled(profile)
        except RcloneError as exc:
            self._fail(profile, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced as-is to the interface
            self._fail(profile, f"Unexpected error: {exc}")

    def _cancelled(self, profile: Profile) -> None:
        """Back to idle without a plan: a truncated analysis is not a summary."""
        state = self.state(profile.id)
        run_id = state.run_id
        job_id = state.job_id
        self._reset(profile.id, "Operation interrupted. Run the analysis again to start over.")
        if job_id is not None:
            try:
                self.rclone.job_stop(job_id)
            except RcloneError:
                pass
            self._wait_job_end(job_id)
        if run_id is not None:
            self.history.close_run(run_id, "cancelled", message="Interrupted by the user.")

    def _fail(self, profile: Profile, message: str) -> None:
        state = self.state(profile.id)
        state.phase = ERROR
        state.last_error = message
        state.message = message
        job_id = state.job_id
        state.job_id = None
        if job_id is not None:
            # The error may come from a monitoring call that failed while
            # the job itself is still running and writing to the shared log.
            # Letting it run would feed its lines to the next analysis.
            try:
                self.rclone.job_stop(job_id)
            except RcloneError:
                pass
            self._wait_job_end(job_id)
        if state.run_id is not None:
            self.history.close_run(state.run_id, "failed", message=message)
            state.run_id = None

    def _wait_job_end(self, job_id: int, timeout: float = 10.0) -> None:
        """Waits until a stopped job has actually finished.

        job/stop is only a request: the job sometimes takes several seconds
        to stop and keeps writing to the shared log meanwhile. Moving on
        without waiting would feed those lines to the next operation.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.rclone.job_status(job_id).get("finished"):
                    return
            except RcloneError:
                return
            time.sleep(0.3)

    # -- execution -----------------------------------------------------------

    def _preflight(self, profile: Profile) -> None:
        if not Path(profile.local).exists():
            raise RcloneError(
                f"Local folder not found: {profile.local}. "
                f"Check that the drive is connected."
            )
        self.rclone.check_remote(profile.remote)

    def _exclude_rules(self, profile: Profile, state: State) -> list[str]:
        """Exclusion list for one run: config patterns plus, when enabled,
        the hidden items found by a fresh scan. Scanned at every run so the
        analysis and the transfer that follows see the same tree."""
        rules = list(self.config.exclude)
        if self.config.skip_hidden:
            found = hidden_excludes(profile.local)
            if state.cancelled:
                raise Cancelled()
            if found:
                log.info("%s: %d hidden item(s) excluded.", profile.id, len(found))
            rules += found
        return rules

    def _options(self) -> dict:
        return {
            "transfers": self.config.transfers,
            "checkers": self.config.checkers,
            "retries": self.config.retries,
            "low_level_retries": self.config.low_level_retries,
            "bandwidth_limit": self.config.bandwidth_limit,
        }

    def _watch(
        self,
        profile: Profile,
        job_id: int,
        group: str,
        tail: LogTail | None,
        counter: "LocalCounter | None" = None,
    ) -> dict:
        """Follows a job to its end, publishing live stats along the way."""
        state = self.state(profile.id)
        deletes: list[str] = []
        copies: list[tuple[str, int]] = []   # (path, size in bytes)
        moved_bytes = 0.0

        while True:
            status = self.rclone.job_status(job_id)
            stats = self.rclone.stats(group)

            # The log is processed before the stats are published: the
            # counters derived from it would otherwise always be one poll
            # behind what the user sees.
            if tail is not None:
                for line in tail.read_new():
                    match = _RE_DELETE.search(line)
                    if match:
                        deletes.append(match.group(1))
                        continue
                    match = _RE_MOVE_SIZE.search(line)
                    if match:
                        moved_bytes += float(match.group(1)) * _UNIT[match.group(2)]
                        continue
                    match = _RE_COPY.search(line)
                    if match:
                        size = 0
                        if match.group(2):
                            size = int(
                                float(match.group(2)) * _UNIT[match.group(3) or ""]
                            )
                        copies.append((match.group(1), size))

            state.live = {
                "bytes": stats.get("bytes", 0),
                "total_bytes": stats.get("totalBytes", 0),
                "speed": stats.get("speed", 0),
                "eta": stats.get("eta"),
                "transfers": stats.get("transfers", 0),
                "total_transfers": stats.get("totalTransfers", 0),
                "checks": stats.get("checks", 0),
                "total_checks": stats.get("totalChecks", 0),
                "renames": stats.get("renames", 0),
                "deletes": stats.get("deletes", 0),
                "errors": stats.get("errors", 0),
                "elapsed": stats.get("elapsedTime", 0),
                # Number of entries rclone has walked. Available in recent
                # versions; absent otherwise, without consequence.
                "listed": stats.get("listed", 0),
                # Denominator computed on our side, the only way to show a
                # percentage while rclone is still building its inventory.
                "local_total": counter.total if counter else 0,
                "local_done": counter.done if counter else False,
                # Counted from the log: during a dry run nothing is actually
                # transferred and the engine's counters stay at zero.
                "seen_deletes": len(deletes),
                "seen_copies": len(copies),
                "checking": [
                    (item if isinstance(item, str) else item.get("name", ""))
                    for item in (stats.get("checking") or [])[:5]
                ],
                "transferring": [
                    {
                        "name": item.get("name", ""),
                        "percentage": item.get("percentage", 0),
                        "speed": item.get("speed", 0),
                        "size": item.get("size", 0),
                    }
                    for item in (stats.get("transferring") or [])[:5]
                ],
            }

            if state.cancelled:
                raise Cancelled()

            if status.get("finished"):
                if state.cancelled:
                    raise Cancelled()
                if not status.get("success"):
                    raise RcloneError(status.get("error") or "The operation failed.")
                return {
                    "stats": stats,
                    "deletes": deletes,
                    "copies": copies,
                    "moved_bytes": int(moved_bytes),
                }

            time.sleep(0.5)

    def _run_analysis(self, profile: Profile, trigger: str) -> Plan:
        state = self.state(profile.id)
        state.phase = ANALYSIS
        state.started_at = time.time()
        state.last_error = ""
        state.blocked_reason = ""
        state.plan = None
        state.message = "Comparing checksums. Nothing is modified."
        self._drop_plan(profile.id)

        self._preflight(profile)
        if state.cancelled:
            raise Cancelled()
        run_id = self.history.open_run(profile.id, profile.name, "analysis", trigger)
        state.run_id = run_id

        exclude_rules = self._exclude_rules(profile, state)

        group = f"analysis-{profile.id}-{int(time.time())}"
        tail = LogTail(self.rclone.log_file)
        counter = LocalCounter(profile.local)
        counter.start()

        job_id = self.rclone.start_sync(
            src=profile.local,
            dst=profile.remote,
            group=group,
            dry_run=True,
            options=self._options(),
            exclude=exclude_rules,
        )
        state.job_id = job_id

        try:
            result = self._watch(profile, job_id, group, tail, counter)
        finally:
            counter.stop()
        stats = result["stats"]
        deletes = result["deletes"]
        moved_bytes = result.get("moved_bytes", 0) or int(
            stats.get("serverSideMoveBytes", 0) or 0
        )
        copies = result.get("copies", [])

        def by_folder(paths) -> list[dict]:
            groups: dict[str, dict] = {}
            for entry in paths:
                path, size = entry if isinstance(entry, tuple) else (entry, 0)
                top = path.split("/", 1)[0] if "/" in path else "(root)"
                g = groups.setdefault(top, {"folder": top, "count": 0, "bytes": 0})
                g["count"] += 1
                g["bytes"] += size
            return sorted(groups.values(), key=lambda d: d["count"], reverse=True)

        plan = Plan(
            moved=int(stats.get("renames", 0)),
            transfers=int(stats.get("totalTransfers", 0)),
            deletes=len(deletes) or int(stats.get("deletes", 0)),
            checks=int(stats.get("totalChecks", 0)),
            # rclone counts moved files in totalBytes: without this
            # subtraction, the plan announces network volume although no
            # byte will leave the machine. And with no copy seen in the log,
            # the volume is zero outright: log sizes are rounded (3 decimals
            # in Ki) and the subtraction would otherwise leave a residue of
            # a few bytes — measured: 3 bytes for 1,000 moves.
            bytes_to_send=(
                0 if not copies
                else max(0, int(stats.get("totalBytes", 0)) - moved_bytes)
            ),
            bytes_moved=moved_bytes,
            delete_samples=deletes,
            delete_by_folder=by_folder(deletes),
            send_samples=[
                {"path": path, "bytes": size}
                for path, size in sorted(copies, key=lambda c: -c[1])
            ],
            send_by_folder=by_folder(copies),
            computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        state.job_id = None
        state.plan = plan
        state.run_id = None
        self.rclone.forget_stats(group)

        self.history.close_run(
            run_id,
            "success",
            stats={
                "moved": plan.moved,
                "transferred": plan.transfers,
                "deleted": plan.deletes,
                "checks": plan.checks,
                "bytes": 0,
                "errors": int(stats.get("errors", 0)),
            },
            details=plan.to_dict(),
        )

        if plan.empty:
            state.phase = IDLE
            state.message = "Both sides already match. Nothing to transfer."
            self._drop_plan(profile.id)
        else:
            state.phase = VALIDATION
            state.message = "Analysis finished. Review the plan before starting the transfer."
            self._save_plan(profile.id, plan)

        state.started_at = None
        return plan

    def _run_transfer(self, profile: Profile, trigger: str, plan: Plan) -> None:
        state = self.state(profile.id)
        state.phase = TRANSFER
        state.started_at = time.time()
        state.message = "Transfer in progress."
        state.live = {}

        self._preflight(profile)
        if state.cancelled:
            raise Cancelled()
        run_id = self.history.open_run(profile.id, profile.name, "transfer", trigger)
        state.run_id = run_id

        exclude_rules = self._exclude_rules(profile, state)

        group = f"transfer-{profile.id}-{int(time.time())}"
        counter = LocalCounter(profile.local)
        counter.start()
        job_id = self.rclone.start_sync(
            src=profile.local,
            dst=profile.remote,
            group=group,
            dry_run=False,
            options=self._options(),
            exclude=exclude_rules,
            # A sync executes current reality, not the plan: the validation
            # screen would otherwise approve one perimeter and the transfer
            # run another. Deletions are the destructive dimension, so they
            # are capped at the validated count — if the tree diverged
            # beyond it, rclone stops instead of widening the perimeter.
            max_delete=plan.deletes,
        )
        state.job_id = job_id

        try:
            result = self._watch(profile, job_id, group, None, counter)
        except RcloneError as exc:
            message = str(exc).lower()
            if "max-delete" in message or "failed to delete" in message:
                raise RcloneError(
                    f"Transfer stopped: it required more deletions than the "
                    f"validated plan allowed ({plan.deletes}). The local tree "
                    f"changed since the analysis; no more than the validated "
                    f"count was deleted. Run a new analysis and review it."
                ) from exc
            raise
        finally:
            counter.stop()
        stats = result["stats"]

        self.history.close_run(
            run_id,
            "success",
            stats={
                "moved": int(stats.get("renames", 0)),
                "transferred": int(stats.get("transfers", 0)),
                "deleted": int(stats.get("deletes", 0)),
                "checks": int(stats.get("checks", 0)),
                "bytes": int(stats.get("bytes", 0)),
                "errors": int(stats.get("errors", 0)),
            },
        )

        self.rclone.forget_stats(group)
        state.job_id = None
        state.run_id = None
        state.plan = None
        state.phase = IDLE
        self._drop_plan(profile.id)
        state.started_at = None
        state.message = (
            f"Done. {int(stats.get('renames', 0))} server-side moves, "
            f"{int(stats.get('transfers', 0))} files uploaded."
        )

    def _run_cycle(self, profile: Profile, trigger: str) -> None:
        plan = self._run_analysis(profile, trigger)
        state = self.state(profile.id)

        if plan.empty:
            return

        if not profile.auto:
            state.blocked_reason = "This profile requires validation before every synchronisation."
            return

        if state.cancelled:
            raise Cancelled()

        if 0 <= profile.max_deletes < plan.deletes:
            state.blocked_reason = (
                f"{plan.deletes} deletions planned, above the threshold of "
                f"{profile.max_deletes} set for this profile. Validation required."
            )
            state.message = "Synchronisation on hold: too many deletions."
            return

        self._run_transfer(profile, trigger, plan)
