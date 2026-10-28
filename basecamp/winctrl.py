"""Send Ctrl+Break (or Ctrl+C) to the console a process is attached to.

Run as its own short-lived process (``python winctrl.py <pid> [break|c]``), never imported into
Basecamp's process: it has to detach from its own console and attach to the game's, and
doing that inside Basecamp would break Basecamp's own console. Standard library only, so
it can run with ``-I`` and no site-packages.

Game servers are started with CREATE_NO_WINDOW, so each one gets its own hidden console.
``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)`` reaches every process on that console:
the game (and its launcher, if still alive) but nothing else on the PC. Ctrl+C reaches
only the ones that haven't turned it off (a game started in a new process group has).

Exit codes: 0 sent, 2 can't attach (process gone or has no console), 3 send failed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1


def send_ctrl_event(pid: int, event: int = CTRL_BREAK_EVENT) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 2
    # We're on the game's console now, so the event reaches us too: swallow it.
    handler = handler_type(lambda _event: True)
    kernel32.SetConsoleCtrlHandler(handler, True)
    ok = kernel32.GenerateConsoleCtrlEvent(event, 0)
    time.sleep(0.2)  # let our own handler thread run before we exit
    kernel32.FreeConsole()
    return 0 if ok else 3


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "break"
    os._exit(send_ctrl_event(int(sys.argv[1]), CTRL_C_EVENT if which == "c" else CTRL_BREAK_EVENT))
