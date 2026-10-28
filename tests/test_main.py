"""basecamp.main: Basecamp exits when its launcher is gone (D-110)."""

from __future__ import annotations

import logging
import os
import subprocess
import threading

import psutil

from basecamp import main
from basecamp.models import Protocol
from basecamp.selfupdate import LAUNCHER_ENV
from basecamp.settings import Settings
from tests.conftest import PYTHON, free_port, wait_for


def test_launcher_process_is_the_parent_only_under_a_launcher(monkeypatch):
    monkeypatch.delenv(LAUNCHER_ENV, raising=False)
    assert main.launcher_process() is None
    monkeypatch.setenv(LAUNCHER_ENV, "1")
    launcher = main.launcher_process()
    assert launcher is not None and launcher.pid == os.getppid()


def test_basecamp_exits_when_its_launcher_is_gone(tmp_path, monkeypatch):
    # Stop-ScheduledTask ends the launcher but not Basecamp; an orphan would keep the port.
    fake_launcher = subprocess.Popen([str(PYTHON), "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(main, "launcher_process", lambda: psutil.Process(fake_launcher.pid))
        settings = Settings.load(tmp_path / "data", port=free_port(Protocol.TCP),
                                 update_check_h=0, low_ram_mb=0)
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(main.run(settings)), daemon=True)
        thread.start()
        wait_for(lambda: (settings.data_dir / "logs" / "basecamp.log").is_file()
                 and "Web page on" in (settings.data_dir / "logs" / "basecamp.log").read_text())
        assert thread.is_alive()
        fake_launcher.kill()
        thread.join(20)
        assert not thread.is_alive() and result == [0]
        assert "The launcher (pid" in (settings.data_dir / "logs" / "basecamp.log").read_text()
    finally:
        fake_launcher.kill()
        for handler in list(logging.getLogger().handlers):
            if getattr(handler, "_basecamp", False):
                logging.getLogger().removeHandler(handler)
                handler.close()
