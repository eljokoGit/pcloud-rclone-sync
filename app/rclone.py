"""Drives rclone through its control API (rclone rcd).

A single rclone daemon is started once, then spoken to over HTTP. This gives
the engine's real statistics (bytes, speed, ETA, server-side moves) instead
of guessing them by re-reading a log file.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


class RcloneError(Exception):
    """Error returned by rclone or the transport. The message is meant for the user."""


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


class RcloneEngine:
    """Wraps the rclone daemon and exposes the operations the app needs."""

    def __init__(
        self,
        binary: str = "rclone",
        port: int = 5577,
        log_dir: Path | str = "logs",
        host: str = "127.0.0.1",
    ) -> None:
        self.binary = binary
        self.host = host
        self.port = port
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "rclone-engine.log"

        self._process: subprocess.Popen | None = None
        self._stderr = None
        self._client = httpx.Client(base_url=f"http://{host}:{port}", timeout=300.0)

    # -- lifecycle -----------------------------------------------------------

    def resolve_binary(self) -> str:
        found = shutil.which(self.binary)
        if not found:
            raise RcloneError(
                f"rclone was not found ({self.binary}). "
                f"Install it with: winget install Rclone.Rclone"
            )
        return found

    def _is_rclone(self) -> bool:
        """Checks that the port is actually held by an rclone engine.

        Attaching to an open port without this check would mean sending our
        sync commands to whatever third-party software happens to listen
        there.
        """
        try:
            response = self._client.post("/core/version", json={}, timeout=3.0)
            return response.status_code == 200 and "version" in response.json()
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> None:
        """Starts the daemon, or attaches to one already listening."""
        if _port_open(self.host, self.port):
            if self._is_rclone():
                return
            raise RcloneError(
                f"Port {self.port} is used by another program. "
                f"Change “rc_port” in config.yaml, for example {self.port + 10}."
            )

        binary = self.resolve_binary()
        args = [
            binary, "rcd",
            "--rc-addr", f"{self.host}:{self.port}",
            "--rc-no-auth",
            "--log-file", str(self.log_file),
            "--log-level", "INFO",
            "--stats", "1s",
            "--stats-log-level", "DEBUG",
        ]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        # stderr goes to a file rather than a pipe: a pipe nobody drains
        # fills up (64 KB) and would block the engine if it got chatty.
        stderr_path = self.log_dir / "rclone-stderr.log"
        self._stderr = open(stderr_path, "ab")
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            creationflags=creationflags,
        )

        for _ in range(200):  # 10 s at most
            if _port_open(self.host, self.port):
                return
            if self._process.poll() is not None:
                try:
                    detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                except OSError:
                    detail = ""
                raise RcloneError(f"The rclone engine did not start:\n{detail.strip()}")
            time.sleep(0.05)

        raise RcloneError("The rclone engine did not respond in time.")

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._stderr is not None:
            try:
                self._stderr.close()
            except OSError:
                pass
        self._client.close()

    # -- transport -------------------------------------------------------------

    def call(self, endpoint: str, payload: dict | None = None) -> dict:
        try:
            response = self._client.post(f"/{endpoint.lstrip('/')}", json=payload or {})
        except httpx.RequestError as exc:
            raise RcloneError(f"The rclone engine is not responding: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            raise RcloneError(f"Unreadable response from the rclone engine (HTTP {response.status_code}).")

        if response.status_code >= 400:
            raise RcloneError(body.get("error") or f"rclone error (HTTP {response.status_code}).")
        return body

    # -- queries ----------------------------------------------------------------

    def version(self) -> str:
        return self.call("core/version").get("version", "unknown")

    def remotes(self) -> list[str]:
        """Names of the configured remotes, normalised without a trailing colon.

        Depending on the version, rclone returns 'pcloud' or 'pcloud:'. The
        matter is settled here so the rest of the code never has to care.
        """
        raw = self.call("config/listremotes").get("remotes", [])
        return [r.rstrip(":") for r in raw]

    def check_remote(self, remote: str) -> None:
        """Checks that a destination's remote exists in the rclone config."""
        name = remote.split(":", 1)[0].rstrip(":")
        available = self.remotes()
        if name not in available:
            listing = ", ".join(available) or "none"
            raise RcloneError(
                f"The remote '{name}' does not exist in rclone. Configured remotes: {listing}. "
                f"Add it with: rclone config"
            )

    # -- jobs --------------------------------------------------------------------

    def start_sync(
        self,
        src: str,
        dst: str,
        group: str,
        dry_run: bool,
        options: dict,
        exclude: list[str],
        max_delete: int | None = None,
    ) -> int:
        """Starts an asynchronous synchronisation. Returns the job id."""
        config = {
            "DryRun": dry_run,
            "TrackRenames": True,
            "TrackRenamesStrategy": "hash",
            "Transfers": options.get("transfers", 8),
            "Checkers": options.get("checkers", 16),
            "Retries": options.get("retries", 5),
            "LowLevelRetries": options.get("low_level_retries", 20),
        }
        if max_delete is not None and max_delete >= 0:
            # Hard ceiling enforced by the engine itself: verified on v1.75,
            # rclone deletes at most this many files, then fails the sync
            # without touching the rest. With a ceiling of 0, nothing is
            # deleted at all.
            config["MaxDelete"] = max_delete
        if options.get("bandwidth_limit"):
            config["BwLimit"] = options["bandwidth_limit"]

        payload = {
            "srcFs": src,
            "dstFs": dst,
            "_async": True,
            "_group": group,
            "_config": config,
        }
        if exclude:
            payload["_filter"] = {"ExcludeRule": exclude}

        return self.call("sync/sync", payload)["jobid"]

    def list_files(self, fs: str, exclude: list[str]) -> dict:
        """Recursive file listing as {path: size}, with filters applied.

        Runs as an asynchronous rc job: listing hundreds of thousands of
        files on a cloud backend can exceed the synchronous client timeout.
        """
        payload = {
            "fs": fs,
            "remote": "",
            "opt": {
                "recurse": True,
                "noModTime": True,
                "noMimeType": True,
                "filesOnly": True,
            },
            "_async": True,
        }
        if exclude:
            payload["_filter"] = {"ExcludeRule": exclude}
        job_id = self.call("operations/list", payload)["jobid"]
        while True:
            status = self.call("job/status", {"jobid": job_id})
            if status.get("finished"):
                if not status.get("success"):
                    raise RcloneError(status.get("error") or "Listing failed.")
                out = status.get("output") or {}
                return {
                    item["Path"]: int(item.get("Size", -1))
                    for item in out.get("list", [])
                }
            time.sleep(0.5)

    def job_status(self, job_id: int) -> dict:
        return self.call("job/status", {"jobid": job_id})

    def job_stop(self, job_id: int) -> None:
        self.call("job/stop", {"jobid": job_id})

    def stats(self, group: str) -> dict:
        return self.call("core/stats", {"group": group})

    def forget_stats(self, group: str) -> None:
        try:
            self.call("core/stats-reset", {"group": group})
        except RcloneError:
            pass
