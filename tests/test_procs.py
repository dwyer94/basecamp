from __future__ import annotations

import os
import socket
import subprocess

import psutil

from basecamp import procs
from basecamp.models import Protocol
from tests.conftest import PYTHON, free_port


def test_port_in_use_udp_and_tcp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("0.0.0.0", 0))
        port = udp.getsockname()[1]
        assert procs.port_in_use(port, Protocol.UDP)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        port = tcp.getsockname()[1]
        assert procs.port_in_use(port, Protocol.TCP)


def test_free_port_is_free():
    assert not procs.port_in_use(free_port(Protocol.UDP), Protocol.UDP)
    assert not procs.port_in_use(free_port(Protocol.TCP), Protocol.TCP)


def test_match_process_checks_exe_and_create_time():
    me = psutil.Process(os.getpid())
    exe, created = me.exe(), me.create_time()

    assert procs.match_process(me.pid, exe, created) is not None
    assert procs.match_process(me.pid, exe, created + 5) is None
    assert procs.match_process(me.pid, r"C:\other\game.exe", created) is None


def test_ctrl_break_to_missing_process_fails_cleanly():
    gone = subprocess.Popen([str(PYTHON), "-c", "pass"])
    gone.wait(10)
    assert procs.send_ctrl_break(gone.pid) is False


def test_free_ram_is_positive():
    assert procs.free_ram_mb() > 0
