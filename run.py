"""Entry point of pCloud Sync.

Usage:
    python run.py                       config.yaml next to the script
    python run.py --config D:\\other.yaml
    python run.py --check               validates the configuration and exits
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from app import config as config_module
from app.db import History
from app.engine import SyncEngine
from app.rclone import RcloneEngine, RcloneError
from app.scheduler import Scheduler
from app.server import create_app
from app.store import ProfileStore

BASE = Path(__file__).parent


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_dir / "app.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="pCloud Sync")
    parser.add_argument("--config", default=str(BASE / "config.yaml"))
    parser.add_argument("--check", action="store_true", help="Validate the configuration and exit")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        cfg = config_module.load(config_module.ensure_config(args.config))
    except config_module.ConfigError as exc:
        print(f"\nInvalid configuration.\n\n{exc}\n", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir)
    log = logging.getLogger("pcloud-sync")

    rclone = RcloneEngine(cfg.rclone_binary, cfg.rc_port, cfg.log_dir)
    try:
        rclone.start()
        version = rclone.version()
        remotes = rclone.remotes()
    except RcloneError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 3

    log.info("rclone engine %s - remotes: %s", version, ", ".join(remotes) or "none")

    store = ProfileStore(cfg.profiles_file)
    imported = store.seed(cfg.seed_profiles)
    if imported:
        log.info("%d profile(s) imported from config.yaml.", imported)

    profiles = store.profiles
    problems: list[str] = []
    for profile in profiles:
        if not Path(profile.local).exists():
            problems.append(f"  {profile.id}: source not found - {profile.local}")
        name = profile.remote.split(":", 1)[0].rstrip(":")
        if name not in remotes:
            problems.append(f"  {profile.id}: remote '{name}' missing from rclone")

    if problems:
        log.warning("Points to check:\n%s", "\n".join(problems))

    if args.check:
        print(f"\nConfiguration: {args.config}")
        print(f"Profiles     : {cfg.profiles_file}")
        print(f"rclone {version} - remotes: {', '.join(remotes) or 'none'}")
        if profiles:
            print(f"\n{len(profiles)} profile(s):")
            for p in profiles:
                mark = f"scheduled {p.schedule}" if p.scheduled else "manual"
                print(f"  - {p.name:<32} {mark}")
        else:
            print("\nNo profiles. They are created from the interface.")
        print("\n" + ("No problems detected." if not problems else "Warnings above."))
        rclone.stop()
        return 0 if not problems else 1

    history = History(cfg.database)
    engine = SyncEngine(cfg, rclone, history, store)
    scheduler = Scheduler(cfg, engine, store)
    scheduler.start()

    app = create_app(cfg, rclone, engine, history, scheduler, store)

    url = f"http://{'127.0.0.1' if cfg.host == '0.0.0.0' else cfg.host}:{cfg.port}"
    log.info("Interface available at %s", url)

    if cfg.open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    def shutdown(*_args):
        log.info("Shutting down.")
        scheduler.stop()
        rclone.stop()
        history.close()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))

    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
    finally:
        shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
