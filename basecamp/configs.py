"""Config files a server lists, edited from the page (R-40..R-43, D-48..D-51).

The guard (R-43): the page names a file by its place on the server's ``config_files`` list,
and every read or write still goes through ``check``, which resolves the path (``..``,
symlinks, junctions, case) and accepts it only if it is the same file as a list entry. The
file itself must not be a link, a folder, or inside Basecamp's data folder (secrets.toml).

Files keep their bytes' shape: the encoding (UTF-8 with or without a BOM, UTF-16 with a
BOM, else a single-byte code page) and line endings are detected on read and used again on
save, so an unchanged file saves byte for byte the same. The page sees ``\\n`` only.

Before each save or restore the old file is copied, byte for byte, to
``<data>\\config-history\\<server id>\\<file name>-<hash>\\YYYYMMDD-HHMMSS-ffffff-<why>.<ext>``;
the newest ``settings.config_copies_keep`` are kept per file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import ntpath
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from basecamp.models import Server
from basecamp.settings import Settings

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024  # bigger than any game config; the page's textarea gets slow
_COPY_NAME = re.compile(r"^(\d{8})-(\d{6})-(\d{6})-(save|restore)(?:-\d+)?(\.[A-Za-z0-9]{1,10})?$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


class ConfigError(Exception):
    """Something the admin can fix (bad JSON, can't encode); the page shows the text."""


class NotAllowed(ConfigError):
    """The path isn't one of the server's config files, or isn't a plain file (R-43)."""


class Conflict(ConfigError):
    """The file changed on disk after the page loaded it."""


# -- encoding and line endings -----------------------------------------------------------


@dataclass(frozen=True)
class Decoded:
    text: str  # as in the file, line endings and all
    encoding: str  # a Python codec name
    bom: bool
    newline: str  # "\r\n" or "\n": what a save writes

    def label(self) -> str:
        enc = {"utf-8": "UTF-8", "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE",
               "cp1252": "Windows-1252", "latin-1": "Latin-1"}.get(self.encoding, self.encoding)
        ends = "CRLF" if self.newline == "\r\n" else "LF"
        return f"{enc}{' with BOM' if self.bom else ''}, {ends}"


def decode(data: bytes) -> Decoded:
    encoding, bom, body = "", False, data
    for mark, name in _BOMS:
        if data.startswith(mark):
            encoding, bom, body = name, True, data[len(mark):]
            break
    if not encoding:
        encoding = _guess(data)
    text = body.decode(encoding)  # _guess only picks codecs that decode it
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\n" if lf > crlf else "\r\n"  # none at all: Windows games expect CRLF
    return Decoded(text, encoding, bom, newline)


def _guess(data: bytes) -> str:
    # UTF-16 without a BOM first: its NULs are valid UTF-8, but no text config has NULs.
    if len(data) >= 4 and len(data) % 2 == 0 and b"\0" in data:
        half = len(data) // 2
        odd_nuls = data[1::2].count(0)
        even_nuls = data[0::2].count(0)
        for nuls, name in ((odd_nuls, "utf-16-le"), (even_nuls, "utf-16-be")):
            if nuls > half * 0.3:
                with contextlib.suppress(UnicodeDecodeError):
                    data.decode(name)
                    return name
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"  # decodes anything


def encode(text: str, like: Decoded) -> bytes:
    """``text`` (any line endings) in the file's own encoding, BOM and line endings."""
    text = text.replace("\r\n", "\n").replace("\n", like.newline)
    try:
        body = text.encode(like.encoding)
    except UnicodeEncodeError as e:
        bad = e.object[e.start:e.end]
        raise ConfigError(f"{bad!r} can't be written in this file's encoding "
                          f"({like.label()}); take it out") from None
    if like.bom:
        body = next(mark for mark, name in _BOMS if name == like.encoding) + body
    return body


def check_json(text: str) -> None:
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Not valid JSON, not saved: {e.msg} at line {e.lineno}, "
                          f"column {e.colno}") from None


def sha256(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


# -- the guard (R-43) ------------------------------------------------------------------------


def entry_path(server: Server, raw: str) -> str:
    """A config list entry (or a path the page sent) as an absolute, normalised path. A
    relative one is taken from the install folder, like the save folder."""
    raw = os.path.expandvars(raw.strip().strip('"'))
    if not raw:
        raise NotAllowed("Empty path")
    if "\0" in raw or ":" in raw[2:] or raw.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        raise NotAllowed("Not a plain file path")  # streams (file:stream), device paths
    if not ntpath.isabs(raw):
        base = server.install_dir or server.working_dir or ntpath.dirname(server.exe_path)
        if not ntpath.isabs(base):
            raise NotAllowed(f"{raw} is relative and the server has no install folder")
        raw = ntpath.join(base, raw)
    return ntpath.normpath(raw)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _is_link(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def _inside(path: str, folder: Path) -> bool:
    base = _real(str(folder))
    with contextlib.suppress(ValueError):  # another drive
        return os.path.commonpath([path, base]) == base
    return False


def check(server: Server, requested: str, settings: Settings) -> Path:
    """The real path of ``requested`` if it is one of the server's config files, else
    ``NotAllowed``. Called on every read, save and restore, not only when listing."""
    target = entry_path(server, requested)
    key = _real(target)
    for raw in server.config_files:
        try:
            if _real(entry_path(server, raw)) == key:
                break
        except NotAllowed:
            continue
    else:
        raise NotAllowed("Not one of this server's config files")
    if _is_link(target):
        raise NotAllowed("This is a link; list the file it points to instead")
    if os.path.isdir(key):
        raise NotAllowed("This is a folder; list the files in it instead")
    if os.path.exists(key) and not os.path.isfile(key):
        raise NotAllowed("This isn't a plain file")
    if _inside(key, settings.data_dir):
        raise NotAllowed("Basecamp's own data folder can't be edited from the page")
    return Path(os.path.realpath(target))


def kind_of(path: str) -> str:
    ext = ntpath.splitext(path)[1].lower()
    return {".json": "json", ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".txt": "text",
            ".toml": "toml", ".yaml": "yaml", ".yml": "yaml"}.get(ext, "text")


# -- the files and their copies ----------------------------------------------------------------


@dataclass(frozen=True)
class Copy:
    name: str
    reason: str  # "save" or "restore": what replaced it
    created: float
    size_bytes: int

    def as_dict(self) -> dict:
        return {"name": self.name, "reason": self.reason, "created": self.created,
                "size_bytes": self.size_bytes}


class ConfigFiles:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()  # one save or restore at a time

    def _path(self, server: Server, n: int) -> Path:
        if not 0 <= n < len(server.config_files):
            raise NotAllowed("No such config file")
        return check(server, server.config_files[n], self._settings)

    def history_dir(self, server: Server, path: Path) -> Path:
        digest = hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:8]
        slug = _SLUG.sub("_", path.name)[:60] or "file"
        return self._settings.data_dir / "config-history" / str(server.id) / f"{slug}-{digest}"

    def list(self, server: Server) -> list[dict]:
        out = []
        for n, raw in enumerate(server.config_files):
            item: dict = {"n": n, "entry": raw, "path": "", "name": ntpath.basename(raw),
                          "kind": kind_of(raw), "exists": False, "size_bytes": None,
                          "modified": None, "error": ""}
            try:
                path = check(server, raw, self._settings)
            except NotAllowed as e:
                item["error"] = str(e)
                out.append(item)
                continue
            item["path"] = str(path)
            with contextlib.suppress(OSError):
                st = path.stat()
                item.update(exists=True, size_bytes=st.st_size, modified=st.st_mtime)
            out.append(item)
        return out

    def read(self, server: Server, n: int) -> dict:
        path = self._path(server, n)
        data = self._read_bytes(path)
        decoded = decode(data) if data is not None else Decoded("", "utf-8", False, "\r\n")
        return {"n": n, "path": str(path), "name": path.name, "kind": kind_of(path.name),
                "exists": data is not None, "text": decoded.text.replace("\r\n", "\n"),
                "encoding": decoded.label(), "sha256": sha256(data),
                "copies": [c.as_dict() for c in self.copies(server, n)]}

    def save(self, server: Server, n: int, text: str, base_sha256: str | None) -> dict:
        """Write ``text`` over config file ``n``. ``base_sha256`` is the file as the page
        loaded it ("" = didn't exist); None skips the check (the admin chose to overwrite)."""
        path = self._path(server, n)
        if kind_of(path.name) == "json":
            check_json(text)
        with self._lock:
            old = self._read_bytes(path)
            if base_sha256 is not None and sha256(old) != base_sha256:
                raise Conflict("The file changed on disk since you opened it (the game may "
                               "have rewritten it)")
            if old is None:
                if not path.parent.is_dir():
                    raise ConfigError(f"Folder not found: {path.parent}")
                like = Decoded("", "utf-8", False, "\r\n")
            else:
                like = decode(old)
            new = encode(text, like)
            if len(new) > MAX_BYTES:
                raise ConfigError("Too big to save from the page")
            copy = self._keep(server, path, old, "save") if old is not None else None
            self._write(path, new)
        return {"saved": path.name, "copy": copy.name if copy else None, "sha256": sha256(new),
                "copies": [c.as_dict() for c in self.copies(server, n)]}

    def copies(self, server: Server, n: int) -> list[Copy]:
        folder = self.history_dir(server, self._path(server, n))
        found = []
        with contextlib.suppress(FileNotFoundError):
            for entry in os.scandir(folder):
                m = _COPY_NAME.match(entry.name)
                if not m or not entry.is_file():
                    continue
                created = time.mktime(datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").timetuple())
                found.append(Copy(entry.name, m[4], created + int(m[3]) / 1e6,
                                  entry.stat().st_size))
        found.sort(key=lambda c: c.name, reverse=True)  # newest first
        return found

    def _copy_path(self, server: Server, n: int, name: str) -> tuple[Path, Path]:
        if not _COPY_NAME.match(name):
            raise NotAllowed(f"Not a copy name: {name!r}")
        path = self._path(server, n)
        copy = self.history_dir(server, path) / name
        if not copy.is_file():
            raise FileNotFoundError(name)
        return path, copy

    def read_copy(self, server: Server, n: int, name: str) -> dict:
        _, copy = self._copy_path(server, n, name)
        data = copy.read_bytes()
        decoded = decode(data)
        return {"name": name, "text": decoded.text.replace("\r\n", "\n"),
                "encoding": decoded.label(), "sha256": sha256(data)}

    def restore(self, server: Server, n: int, name: str) -> dict:
        """Put a copy back, byte for byte. The file it replaces is kept as a copy first."""
        path, copy = self._copy_path(server, n, name)
        with self._lock:
            data = copy.read_bytes()
            old = self._read_bytes(path)
            kept = self._keep(server, path, old, "restore") if old is not None else None
            self._write(path, data)
        return {"restored": name, "copy": kept.name if kept else None, "sha256": sha256(data),
                "copies": [c.as_dict() for c in self.copies(server, n)]}

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _read_bytes(path: Path) -> bytes | None:
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ConfigError(f"{path.name} is over {MAX_BYTES // 1024 // 1024} MB, too "
                                  "big for the page's editor")
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError as e:
            raise ConfigError(f"Can't read {path.name}: {e.strerror}") from None

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        tmp = path.with_name(f"{path.name}.basecamp-tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ConfigError(f"Can't write {path.name}: {e.strerror or e}") from None

    def _keep(self, server: Server, path: Path, data: bytes, reason: str) -> Copy:
        folder = self.history_dir(server, path)
        folder.mkdir(parents=True, exist_ok=True)
        ext = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", path.suffix) else ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        name = f"{stamp}-{reason}{ext}"
        i = 1
        while (folder / name).exists():
            name = f"{stamp}-{reason}-{i}{ext}"
            i += 1
        (folder / name).write_bytes(data)
        keep = self._settings.config_copies_keep
        for n, entry in enumerate(sorted((e for e in folder.iterdir()
                                          if _COPY_NAME.match(e.name)), reverse=True)):
            if n >= keep:
                with contextlib.suppress(OSError):
                    entry.unlink()
        log.info("Kept a copy of %s for server %s: %s", path.name, server.id, name)
        return Copy(name, reason, time.time(), len(data))
