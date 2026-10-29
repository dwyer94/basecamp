"""Fake game server for tests and dev. Standard library only.

Behaves like a dedicated server: prints lines, binds a port, reads commands on stdin,
saves and quits on ``quit``, Ctrl+Break or Ctrl+C, and can be told to crash or hang.

    python tools/fake_server.py --port 27015 --ready-delay 1

stdin commands:
    save                      print a save message
    quit | stop | shutdown    save and exit 0
    crash                     exit 3 at once (an unexpected exit)
    hang                      stop responding: ignore quit and Ctrl+Break (only kill works)
    say <text>                print <text>

Options mimic awkward real games:
    --log-file PATH    write output to a file instead of stdout (like Enshrouded)
    --spawn-child      act as a launcher: start a copy of itself, then exit (like Palworld)
    --hang-on-stop     same as typing ``hang`` at start
    --crash-after S    exit 3 after S seconds
    --autosave S       write <save-dir>/world.sav every S seconds (like Dragonwilds)
    --save-dir PATH    where --autosave writes (default: the working folder)
    --write-config P   once up, write a settings file at P if it isn't there (like most
                       games' first start): an INI with an empty OwnerId, or JSON for .json

Like a real game, it keeps running if whoever reads its output goes away: write errors on
a broken stdout pipe are ignored, and EOF on stdin just stops reading commands.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time


class Output:
    def __init__(self, log_file: str | None):
        self._lock = threading.Lock()
        self._file = open(log_file, "a", encoding="utf-8") if log_file else None  # noqa: SIM115
        self._broken = False

    def say(self, text: str) -> None:
        with self._lock:
            if self._file is not None:
                self._file.write(text + "\n")
                self._file.flush()
                return
            if self._broken:
                return
            try:
                sys.stdout.buffer.write((text + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                self._broken = True  # reader is gone (e.g. Basecamp was killed); keep running


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fake game server")
    parser.add_argument("--name", default="FakeGame")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--proto", choices=["udp", "tcp"], default="udp")
    parser.add_argument("--ready-delay", type=float, default=0.2)
    parser.add_argument("--save-delay", type=float, default=0.2)
    parser.add_argument("--tick", type=float, default=0, help="print a heartbeat every N s")
    parser.add_argument("--log-file")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--hang-on-stop", action="store_true")
    parser.add_argument("--crash-after", type=float, default=0)
    parser.add_argument("--autosave", type=float, default=0)
    parser.add_argument("--save-dir", default=".")
    parser.add_argument("--write-config")
    return parser.parse_args(argv)


def run_launcher(argv: list[str], out: Output) -> int:
    child_args = [a for a in argv if a != "--spawn-child"]
    # Pass our std handles on explicitly so the child shares the pipes, like real launchers.
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), *child_args],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
    )
    out.say(f"Launcher: started server process {child.pid}")
    time.sleep(1.0)
    out.say("Launcher: exiting")
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    out = Output(args.log_file)
    if args.spawn_child:
        return run_launcher(argv, out)

    quit_event = threading.Event()
    state = {"hung": args.hang_on_stop, "code": 0}

    def save() -> None:
        out.say("Saving world...")
        time.sleep(args.save_delay)
        out.say("World saved")

    def on_break(_signum: int, _frame: object) -> None:
        if state["hung"]:
            out.say("(hung) ignoring Ctrl+Break")
            return
        out.say("Ctrl+Break received, shutting down")
        quit_event.set()

    def on_ctrl_c(_signum: int, _frame: object) -> None:
        if state["hung"]:
            out.say("(hung) ignoring Ctrl+C")
            return
        out.say("Ctrl+C received, shutting down")
        quit_event.set()

    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_break)
    signal.signal(signal.SIGINT, on_ctrl_c)

    def autosave() -> None:
        path = os.path.join(args.save_dir, "world.sav")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"saved {time.time()}\n")
        out.say(f"Autosaved {path}")

    def read_commands() -> None:
        for raw in sys.stdin:
            cmd, _, rest = raw.strip().partition(" ")
            cmd = cmd.lower()
            if not cmd:
                continue
            if state["hung"]:
                continue
            if cmd == "save":
                save()
            elif cmd in ("quit", "stop", "shutdown"):
                out.say("Shutdown requested")
                quit_event.set()
            elif cmd == "crash":
                out.say("Fatal error: simulated crash")
                os._exit(3)
            elif cmd == "hang":
                out.say("Hanging (ignoring everything from now on)")
                state["hung"] = True
            elif cmd == "say":
                out.say(rest)
            else:
                out.say(f"Unknown command: {cmd}")
        # stdin closed: carry on without commands, as games do.

    out.say(f"{args.name} starting (pid {os.getpid()})")
    sock = None
    if args.port:
        kind = socket.SOCK_DGRAM if args.proto == "udp" else socket.SOCK_STREAM
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind(("0.0.0.0", args.port))
        except OSError as e:
            out.say(f"Fatal error: can't bind {args.proto} port {args.port}: {e}")
            return 2
        if args.proto == "tcp":
            sock.listen()
        out.say(f"Listening on {args.proto} port {args.port}")

    threading.Thread(target=read_commands, daemon=True).start()
    time.sleep(args.ready_delay)
    if args.write_config and not os.path.exists(args.write_config):
        os.makedirs(os.path.dirname(os.path.abspath(args.write_config)), exist_ok=True)
        with open(args.write_config, "w", encoding="utf-8", newline="") as f:
            if args.write_config.lower().endswith(".json"):
                f.write('{\n  "ServerName": "Fake",\n  "MaxPlayers": 4,\n  "Owner": ""\n}\n')
            else:
                f.write("[/Script/Fake.Settings]\r\nServerName=Fake\r\nOwnerId=\r\n")
        out.say(f"Wrote default settings to {args.write_config}")
    out.say(f"Server ready on port {args.port}")

    started = time.monotonic()
    last_tick = last_save = started
    while not quit_event.wait(0.1):
        now = time.monotonic()
        if args.crash_after and now - started >= args.crash_after:
            out.say("Fatal error: simulated crash")
            os._exit(3)
        if args.tick and now - last_tick >= args.tick:
            last_tick = now
            out.say(f"tick {now - started:.0f}s")
        if args.autosave and now - last_save >= args.autosave:
            last_save = now
            autosave()

    save()
    out.say("Goodbye")
    if sock is not None:
        sock.close()
    return state["code"]


if __name__ == "__main__":
    code = main(sys.argv[1:])
    # Skip interpreter teardown: flushing a broken stdout pipe at exit would raise.
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    os._exit(code)
