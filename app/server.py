"""HTTP API and static file serving for the interface."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import browse
from .config import Config
from .db import History
from .engine import SyncEngine
from .rclone import RcloneEngine, RcloneError
from .scheduler import Scheduler
from .store import ProfileError, ProfileStore
from .update import Updater

log = logging.getLogger("pcloud-sync.server")
STATIC = Path(__file__).parent / "static"


def create_app(
    config: Config,
    rclone: RcloneEngine,
    engine: SyncEngine,
    history: History,
    scheduler: Scheduler,
    store: ProfileStore,
) -> FastAPI:
    app = FastAPI(title="pCloud Sync", docs_url=None, redoc_url=None)
    updater = Updater(STATIC.parent.parent, __import__("app").__version__)

    # The API has no authentication: it trusts whoever reaches it. Without
    # these checks, any web page visited by the user could fire the
    # parameterless POSTs (analyse, sync, run, cancel) as cross-origin
    # "simple requests" that browsers send without a CORS preflight — a
    # malicious page could start a transfer that was awaiting validation.
    # The Host check also blocks DNS-rebinding, which would let a remote
    # page read the API as if it were same-origin.
    if config.host in ("127.0.0.1", "localhost"):
        allowed_hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}"}
    else:
        # Bound to a LAN address or 0.0.0.0: the reachable names are not
        # known in advance. The Origin check below still applies; exposing
        # an unauthenticated interface is the user's explicit choice,
        # documented in config.example.yaml.
        allowed_hosts = None

    @app.middleware("http")
    async def _origin_guard(request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse(
                status_code=403,
                content={"error": "Request refused: unexpected Host header."},
            )
        if request.method in ("POST", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            # Browsers always send Origin on cross-origin requests; tools
            # like curl send none and are the local user's own doing.
            if origin and urlparse(origin).netloc != host:
                return JSONResponse(
                    status_code=403,
                    content={"error": "Cross-origin requests are not allowed."},
                )
        return await call_next(request)

    @app.exception_handler(RcloneError)
    async def _rclone_error(_request, exc: RcloneError):
        return JSONResponse(status_code=502, content={"error": str(exc)})

    @app.exception_handler(ProfileError)
    async def _profile_error(_request, exc: ProfileError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # -- interface -------------------------------------------------------------

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # -- state -----------------------------------------------------------------

    @app.get("/api/state")
    async def state():
        profiles = engine.snapshot()
        for pid, data in profiles.items():
            data["next_run"] = scheduler.next_run(pid)
        return {
            "profiles": profiles,
            "order": [p.id for p in store.profiles],
            "totals": history.totals(),
        }

    @app.get("/api/ping")
    async def ping():
        """Application signature.

        Tells our server apart from other software that may hold the same
        port: without this check, a second launch would open the neighbour's
        interface believing it found its own.
        """
        return {"app": "pcloud-sync", "version": __import__("app").__version__}

    @app.get("/api/update")
    def update_info(force: int = 0):
        # Plain def: FastAPI runs it in a thread, the network call must not
        # block the event loop that serves /api/state every second.
        if not config.update_check and not force:
            return {"available": False, "disabled": True, "status": updater.status}
        info = updater.check(force=bool(force))
        return {**info, "status": updater.status}

    def _relaunch() -> None:
        # Let the HTTP response leave before the process dies, then spawn
        # the same command line detached and exit. The child waits for the
        # port to free (PCLOUDSYNC_RESTART) before its "already running"
        # check — without that it would find this dying instance and open
        # its interface instead of starting.
        time.sleep(0.5)
        log.info("Restarting on user request.")
        for stop in (scheduler.stop, rclone.stop, history.close):
            try:
                stop()
            except Exception:  # noqa: BLE001 - dying anyway, restart first
                pass
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable] + sys.argv,
            cwd=os.getcwd(),
            env={**os.environ, "PCLOUDSYNC_RESTART": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        os._exit(0)

    @app.post("/api/restart")
    def restart():
        # Same guard as the update: killing the process under a running
        # transfer would leave a half-finished sync behind.
        for p in store.profiles:
            if engine.busy(p.id):
                raise HTTPException(409, "Finish or stop running operations before restarting.")
        threading.Thread(target=_relaunch, daemon=True).start()
        return {"ok": True}

    @app.post("/api/update/apply")
    def update_apply():
        # Replacing files under a running transfer would be asking for
        # trouble; the update waits for a quiet engine.
        for p in store.profiles:
            if engine.busy(p.id):
                raise HTTPException(409, "Finish or stop running operations before updating.")
        if not updater.start_apply():
            raise HTTPException(409, "No update available, or one is already in progress.")
        return {"ok": True}

    @app.get("/api/engine")
    async def engine_info():
        try:
            return {"ok": True, "version": rclone.version(), "remotes": rclone.remotes()}
        except RcloneError as exc:
            return {"ok": False, "error": str(exc)}

    # -- browsing ----------------------------------------------------------------

    @app.get("/api/browse/local")
    async def browse_local(path: str = ""):
        try:
            return browse.list_local(path or None)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/browse/remote")
    async def browse_remote(remote: str = "", path: str = ""):
        return browse.list_remote(rclone, remote or None, path)

    @app.get("/api/suggest-remote")
    async def suggest_remote(local: str, base: str):
        return {"remote": browse.suggest_remote(local, base)}

    # -- profiles ----------------------------------------------------------------

    @app.get("/api/profiles")
    async def list_profiles():
        return {"profiles": [p.to_dict() for p in store.profiles]}

    @app.post("/api/profiles")
    async def create_profile(payload: dict = Body(...)):
        entries = payload.get("profiles")
        if isinstance(entries, list):
            created = store.add_many(entries)
            return {"created": [p.to_dict() for p in created]}
        return {"created": [store.add(payload).to_dict()]}

    @app.patch("/api/profiles/{profile_id}")
    async def edit_profile(profile_id: str, payload: dict = Body(...)):
        if engine.busy(profile_id):
            raise HTTPException(409, "An operation is running on this profile.")
        before = store.get(profile_id)
        updated = store.update(profile_id, payload)
        # A plan awaiting validation describes the source and destination as
        # they were at analysis time: if either changes, it describes nothing
        # anymore, and keeping it would let a never-analysed transfer pass
        # for a validated one.
        if before is not None and (
            updated.local != before.local or updated.remote != before.remote
        ):
            engine.discard_plan(profile_id)
        return updated.to_dict()

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str):
        if engine.busy(profile_id):
            raise HTTPException(409, "An operation is running on this profile.")
        store.remove(profile_id)
        engine.forget(profile_id)
        return {"ok": True}

    def _exists(profile_id: str):
        if store.get(profile_id) is None:
            raise HTTPException(404, "Unknown profile.")

    @app.get("/api/profiles/{profile_id}/plan")
    async def full_plan(profile_id: str):
        """Complete plan, untruncated lists. Served on demand."""
        _exists(profile_id)
        plan = engine.state(profile_id).plan
        if plan is None:
            raise HTTPException(404, "No plan is pending for this profile.")
        return plan.to_dict(full=True)

    # -- actions -----------------------------------------------------------------

    def _command(profile_id: str, fn):
        _exists(profile_id)
        try:
            fn(profile_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/profiles/{profile_id}/analyse")
    async def analyse(profile_id: str):
        return _command(profile_id, engine.analyse)

    @app.post("/api/profiles/{profile_id}/sync")
    async def sync(profile_id: str):
        return _command(profile_id, engine.synchronise)

    @app.post("/api/profiles/{profile_id}/run")
    async def run(profile_id: str):
        return _command(profile_id, engine.full_cycle)

    @app.post("/api/profiles/{profile_id}/cancel")
    async def cancel(profile_id: str):
        return _command(profile_id, engine.cancel)

    # -- history -----------------------------------------------------------------

    @app.get("/api/history")
    async def get_history(limit: int = 60, profile: str | None = None):
        return {"runs": history.recent(limit=min(limit, 300), profile_id=profile)}

    @app.get("/api/history/{run_id}")
    async def get_run(run_id: int):
        run = history.get(run_id)
        if run is None:
            raise HTTPException(404, "Run not found.")
        return run

    @app.delete("/api/history/{run_id}")
    async def delete_run(run_id: int):
        if not history.delete(run_id):
            raise HTTPException(404, "Run not found.")
        return {"ok": True}

    @app.delete("/api/history")
    async def clear_history(profile: str | None = None, keep_last: int = 0):
        deleted = history.clear(profile_id=profile, keep_last=max(0, keep_last))
        return {"ok": True, "deleted": deleted}

    return app
