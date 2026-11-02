"""A home's model package and where it is kept. No Home Assistant imports: plain blocking file work, run in the executor.

A model package is a zip named `home3d-model-HOME-VERSION.zip` holding `home.json` (at least `id`) and `site/` (the
static page the panel shows, `site/index.html` first). docs/model-package.md has the full format.

Installed models live in one folder per home (in Home Assistant's cache folder, which backups leave out: a model can
always be downloaded again), one subfolder per version, and the file `current` names the one in use. A new version is
unpacked beside the current one, written to disk, and only then made current by an atomic rename, so a failed or
interrupted install never touches the model being served. The previous version is kept; older ones are removed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import zipfile

_LOGGER = logging.getLogger(__name__)

HOME = re.compile(r"[a-z0-9][a-z0-9_]*")
ASSET = re.compile(r"home3d-model-(?P<home>[a-z0-9][a-z0-9_]*)-(?P<version>\d[0-9A-Za-z.+-]*)\.zip")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")
MAX_UNPACKED = 1024**3  # bytes; refuses a zip that would unpack to more (a zip bomb, or the wrong file)
MAX_FILES = 100_000
FLOOR = 500 * 1024**2  # bytes left free after an install, for Home Assistant's own database and logs


class PackageError(Exception):
    """The package is not a valid Home 3D model package, or it does not fit."""


def parse_asset(name: str) -> tuple[str, str] | None:
    """(home, version) of a release asset that is a model package, else None."""
    m = ASSET.fullmatch(name)
    return (m["home"], m["version"]) if m else None


class ModelStore:
    """The installed versions of one home's model."""

    def __init__(self, root: Path, home: str) -> None:
        if not HOME.fullmatch(home):  # the folder is removed with the entry: never anything but a home's own
            raise ValueError(f"invalid home id {home!r}")
        self.root = root / home
        self.home = home

    def _valid(self, version: str) -> bool:
        return bool(VERSION.fullmatch(version)) and (self.root / version / "site" / "index.html").is_file()

    def current_version(self) -> str | None:
        """The version in use; if `current` is unreadable, the newest complete version on disk."""
        try:
            version = (self.root / "current").read_text(encoding="utf-8").strip()
            if self._valid(version):
                return version
        except (OSError, ValueError):
            pass
        found = [p.name for p in self.root.iterdir() if p.is_dir() and self._valid(p.name)] if self.root.is_dir() else []
        if found:
            newest = max(found, key=lambda p: (self.root / p).stat().st_mtime)
            _LOGGER.warning("%s: 'current' unreadable, using %s", self.root, newest)
            return newest
        return None

    def site(self) -> Path | None:
        version = self.current_version()
        return (self.root / version / "site").resolve() if version else None

    def home_json(self) -> dict[str, Any] | None:
        version = self.current_version()
        if version is None:
            return None
        try:
            meta = json.loads((self.root / version / "home.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        meta["version"] = version
        return meta

    def new_download(self, need: int) -> Path:
        """A fresh file for a download, after checking the space for it."""
        self.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.root).free
        if need and free < need + FLOOR:
            raise PackageError(f"not enough disk space: {(need + FLOOR) // 1024**2} MiB needed, {free // 1024**2} MiB free")
        fd, name = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=self.root)
        os.close(fd)
        return Path(name)

    def install(self, zip_path: Path, version: str) -> dict[str, Any]:
        """Unpack a downloaded package, check it, make it current, prune. Returns its home.json."""
        if not VERSION.fullmatch(version):
            raise PackageError(f"invalid version {version!r}")
        staging = Path(tempfile.mkdtemp(prefix=".new-", dir=self.root))
        try:
            _unpack(zip_path, staging)
            meta = _check(staging, self.home)
            _sync(staging)
            target = self.root / version
            if target.exists():  # the same version again (a repair): swap it out whole
                old = Path(tempfile.mkdtemp(prefix=".old-", dir=self.root))
                os.replace(target, old / version)
            os.replace(staging, target)
            previous = self.current_version()
            tmp = self.root / "current.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(version + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.root / "current")
            _fsync_dir(self.root)
            self._prune(keep={version, previous})
            meta["version"] = version
            return meta
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            zip_path.unlink(missing_ok=True)

    def sweep(self) -> None:
        """Remove what an interrupted install left behind (call while no install runs)."""
        if self.root.is_dir():
            self._prune(keep={self.current_version(), *(p.name for p in self.root.iterdir() if self._valid(p.name))})

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _prune(self, keep: set[str | None]) -> None:
        for p in self.root.iterdir():
            if p.name == "current":
                continue
            if p.is_dir() and p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() and (p.name.startswith(".") or p.name == "current.tmp"):
                p.unlink(missing_ok=True)


def _unpack(zip_path: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES:
                raise PackageError(f"more than {MAX_FILES} files")
            size = sum(m.file_size for m in members)
            if size > MAX_UNPACKED:
                raise PackageError(f"unpacks to more than {MAX_UNPACKED // 1024**2} MiB")
            free = shutil.disk_usage(dest).free
            if free < size + FLOOR:
                raise PackageError(f"not enough disk space to unpack: {(size + FLOOR) // 1024**2} MiB needed, {free // 1024**2} MiB free")
            base = dest.resolve()
            for m in members:
                target = (dest / m.filename).resolve()
                if not target.is_relative_to(base) or m.filename.startswith("/") or (m.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PackageError(f"unsafe path {m.filename!r}")
            archive.extractall(dest)
    except PackageError:
        raise
    except Exception as err:  # a corrupt member (zlib, CRC, EOF), not a zip, a full disk
        raise PackageError(f"broken package: {err}") from err


def _check(dest: Path, home: str) -> dict[str, Any]:
    try:
        meta = json.loads((dest / "home.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise PackageError("no readable home.json") from err
    if not isinstance(meta, dict) or meta.get("id") != home:
        raise PackageError(f"home.json is not for the home {home!r}")
    if not (dest / "site" / "index.html").is_file():
        raise PackageError("no site/index.html")
    return meta


def _sync(folder: Path) -> None:
    """Every unpacked file on disk before the version becomes current (an SD card may lose a power cut)."""
    for path, _dirs, files in os.walk(folder):
        for name in files:
            fd = os.open(os.path.join(path, name), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(Path(path))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # not every filesystem syncs a directory
        pass
    finally:
        os.close(fd)
