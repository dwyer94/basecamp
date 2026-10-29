"""Per-server console as Basecamp sees it (R-23): the last lines in memory and live
subscribers (the console WebSocket in Phase 2, ready-line detection in the supervisor).

While a server runs, its host process (basecamp/host.py) owns the console log file and
Basecamp mirrors the host's lines here without writing them again (``to_file=False``).
When no host is running (stopped, or a bare adopted game), Basecamp writes its own notes
to the same file.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from basecamp.host import FileTailer, RotatingFile, format_log_line

__all__ = ["Console", "ConsoleLine", "FileTailer", "LineSource", "RotatingFile"]

log = logging.getLogger(__name__)


class LineSource(StrEnum):
    OUT = "out"  # the game's own output
    IN = "in"  # a command we sent
    SYS = "sys"  # a note from Basecamp ("started", "killed", ...)
    STEAM = "steam"  # SteamCMD output during an install or update (basecamp/steam.py)


@dataclass(frozen=True)
class ConsoleLine:
    seq: int
    ts: float
    source: LineSource
    text: str


Subscriber = Callable[[ConsoleLine], None]


class Console:
    def __init__(self, log_path: Path, max_lines: int = 2000,
                 max_bytes: int = 10 * 1024 * 1024, backups: int = 5):
        self._lines: deque[ConsoleLine] = deque(maxlen=max_lines)
        self._file = RotatingFile(log_path, max_bytes, backups)
        self._lock = threading.Lock()
        self._subs: list[Subscriber] = []
        self._seq = 0

    @property
    def log_path(self) -> Path:
        return self._file.path

    def write(self, text: str, source: LineSource = LineSource.OUT, *,
              ts: float | None = None, to_file: bool = True) -> ConsoleLine:
        with self._lock:
            self._seq += 1
            line = ConsoleLine(self._seq, time.time() if ts is None else ts, source, text)
            self._lines.append(line)
            if to_file:
                try:
                    self._file.write(format_log_line(line.ts, source.value, text))
                except OSError as e:
                    log.warning("Can't write console log %s: %s", self._file.path, e)
                finally:
                    self._file.close()  # don't hold the file: a host may write it next
            subs = list(self._subs)
        for sub in subs:
            try:
                sub(line)
            except Exception:
                log.exception("Console subscriber failed")
        return line

    def system(self, text: str, *, to_file: bool = True) -> ConsoleLine:
        return self.write(text, LineSource.SYS, to_file=to_file)

    def last_text(self, source: LineSource | None = None) -> str | None:
        """The newest line's text (of that source), for a crashed server's card."""
        with self._lock:
            for line in reversed(self._lines):
                if source is None or line.source is source:
                    return line.text
        return None

    def lines(self, after_seq: int = 0) -> list[ConsoleLine]:
        with self._lock:
            return [ln for ln in self._lines if ln.seq > after_seq]

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def close(self) -> None:
        with self._lock:
            self._file.close()
