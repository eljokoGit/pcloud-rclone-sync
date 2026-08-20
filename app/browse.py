"""Browsing of local sources and remote destinations.

Used to pick a drive or folder from the interface, without typing a path by
hand.
"""

from __future__ import annotations

import ctypes
import os
import string
import sys
from pathlib import Path

from .rclone import RcloneEngine, RcloneError

import re

WINDOWS = sys.platform == "win32"

# Trailing "Drive X" segment, as created by the pCloud client
_DRIVE_TAIL = re.compile(r"/Drive [A-Za-z]$")

# System folders there is no reason to offer as a source
IGNORED = {
    "$recycle.bin",
    "system volume information",
    "$windows.~ws",
    "$windows.~bt",
    "recovery",
    "config.msi",
    "$sysreset",
}


def _volume_label(letter: str) -> str:
    """Windows volume name, empty string when unavailable."""
    if not WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(f"{letter}:\\"),
            buf, ctypes.sizeof(buf) // 2,
            None, None, None,
            fs, ctypes.sizeof(fs) // 2,
        )
        return buf.value if ok else ""
    except Exception:  # noqa: BLE001
        return ""


def _free_space(path: str) -> dict:
    try:
        usage = os.statvfs(path) if not WINDOWS else None
        if usage is not None:
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            return {"total": total, "free": free}
    except (OSError, AttributeError):
        pass

    if WINDOWS:
        try:
            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(path),
                ctypes.pointer(free_bytes),
                ctypes.pointer(total_bytes),
                None,
            )
            return {"total": total_bytes.value, "free": free_bytes.value}
        except Exception:  # noqa: BLE001
            pass
    return {}


def drives() -> list[dict]:
    """Available drives. On Windows, the mounted letters."""
    if not WINDOWS:
        roots = [Path("/"), Path.home()]
        return [
            {
                "path": str(r),
                "label": "Root" if str(r) == "/" else "Home folder",
                "kind": "drive",
                **_free_space(str(r)),
            }
            for r in roots
            if r.exists()
        ]

    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        label = _volume_label(letter)
        found.append(
            {
                "path": root,
                "label": f"{label} ({letter}:)" if label else f"Drive {letter}:",
                "kind": "drive",
                **_free_space(root),
            }
        )
    return found


def list_local(path: str | None) -> dict:
    """Sub-folders of a local path. Without a path, returns the drives."""
    if not path:
        return {"path": "", "parent": None, "entries": drives()}

    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not target.is_dir():
        raise NotADirectoryError(f"Not a folder: {path}")

    entries = []
    try:
        for item in os.scandir(target):
            if not item.is_dir(follow_symlinks=False):
                continue
            if item.name.lower() in IGNORED or item.name.startswith("$"):
                continue
            entries.append({"path": item.path, "label": item.name, "kind": "folder"})
    except PermissionError:
        raise PermissionError(f"Access denied: {path}")

    entries.sort(key=lambda e: e["label"].lower())

    parent = str(target.parent) if target.parent != target else None
    if WINDOWS and parent and len(str(target)) <= 3:
        parent = ""   # from a drive root, go back up to the drive list

    return {"path": str(target), "parent": parent, "entries": entries}


def list_remote(rclone: RcloneEngine, remote: str | None, path: str = "") -> dict:
    """Sub-folders of an rclone destination. Without a remote, returns the remotes."""
    if not remote:
        return {
            "remote": "",
            "path": "",
            "parent": None,
            "entries": [
                {"path": f"{name}:", "label": name, "kind": "remote"}
                for name in rclone.remotes()
            ],
        }

    fs = remote if remote.endswith(":") else f"{remote}:"
    # Only trailing slashes are removed: an absolute path must keep its
    # leading one, otherwise local backends start from the current folder.
    clean = path.rstrip("/")

    # The full path goes into "fs" instead of being split into fs + remote:
    # some backends, the local backend included, anchor their root somewhere
    # unexpected and a relative path becomes unreachable there.
    try:
        result = rclone.call(
            "operations/list",
            {"fs": f"{fs}{clean}", "remote": "", "opt": {"dirsOnly": True, "noModTime": True}},
        )
    except RcloneError as exc:
        raise RcloneError(f"Cannot read {fs}{clean}: {exc}") from exc

    prefix = f"{clean.rstrip('/')}/" if clean else ""
    entries = [
        {
            "path": f"{prefix}{item['Name']}",
            "label": item["Name"],
            "kind": "folder",
            "full": f"{fs}{prefix}{item['Name']}",
        }
        for item in result.get("list", [])
        if item.get("IsDir")
    ]
    entries.sort(key=lambda e: e["label"].lower())

    parent = None
    if clean:
        parent = clean.rsplit("/", 1)[0] if "/" in clean.lstrip("/") else ""

    return {"remote": fs, "path": clean, "parent": parent, "entries": entries}


def suggest_remote(local: str, base: str) -> str:
    """Suggests a destination that mirrors the local tree under a base.

    D:\\Photos  +  pcloud:pCloud Backup/MY-PC
      -> pcloud:pCloud Backup/MY-PC/Drive D/Photos

    This is the convention the pCloud client itself uses, which makes it
    possible to plug into an existing backup without re-uploading anything.
    """
    base = base.rstrip("/")
    normalised = local.replace("\\", "/")

    if len(normalised) >= 2 and normalised[1] == ":":
        letter = normalised[0].upper()
        rest = normalised[2:].strip("/")
        # If the base already points at a drive folder, it is replaced
        # rather than stacked: browsing to "Drive D" then picking G: must
        # not produce "Drive D/Drive G".
        base = _DRIVE_TAIL.sub("", base)
        base = f"{base}/Drive {letter}"
        return f"{base}/{rest}" if rest else base

    rest = normalised.strip("/")
    return f"{base}/{rest}" if rest else base
