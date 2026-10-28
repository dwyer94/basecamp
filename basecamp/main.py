"""App start: settings, logging, migrations, adopt orphans, start-on-boot servers, and the
web server on ``settings.host:settings.port`` (127.0.0.1 only, D-27).
"""

from __future__ import annotations

import json
import logging
import threading

import psutil
import uvicorn

from basecamp import __version__
from basecamp.api.app import create_app
from basecamp.backups import Backups
from basecamp.crashes import CrashPolicy
from basecamp.db import Store
from basecamp.livestatus import LiveStatus
from basecamp.logs import setup_logging
from basecamp.notify import Notifier
from basecamp.selfupdate import SelfUpdater
from basecamp.settings import Settings
from basecamp.steam import SteamRunner
from basecamp.supervisor import Supervisor

log = logging.getLogger(__name__)


def run(settings: Settings | None = None, stop: threading.Event | None = None) -> int:
    """Runs until ``stop`` is set; returns the exit code (3 = the launcher should start the
    update named in pending.txt, docs/07-updates.md)."""
    settings = settings or Settings.load()
    stop = stop or threading.Event()
    setup_logging(settings)
    log.info("Basecamp %s starting, data in %s", __version__, settings.data_dir)
    store = Store.open(settings.db_path, settings.migrations_dir)
    supervisor = Supervisor(store, settings)
    notifier = Notifier(settings, store)
    supervisor.add_listener(notifier.on_state)
    # Before adopting: a run that crashed while Basecamp was down is restarted too (R-05).
    crashes = CrashPolicy(store, supervisor, settings, notifier)
    backups = Backups(store, supervisor, settings, notifier.server_event)
    steam = SteamRunner(store, supervisor, settings, backups, notifier)
    live = LiveStatus(settings, store, supervisor)
    adopted = supervisor.adopt_orphans()
    if adopted:
        log.info("Adopted %d running server(s)", len(adopted))
    boot = threading.Thread(target=supervisor.start_on_boot, kwargs={"cancel": stop},
                            name="start on boot", daemon=True)
    boot.start()
    if first_start_since_boot(settings):
        names = [s.name for s in store.list_servers() if s.start_on_boot]
        notifier.post(":arrow_forward: Basecamp started after the PC restarted"
                      + (f"; starting {', '.join(names)}" if names else ""))
    backups.start_scheduler()
    steam.start_scheduler()
    live.poke()  # the servers as they are after adopting
    updater = SelfUpdater(settings, store, notifier, stop)
    updater.start()
    app = create_app(store, supervisor, settings, notifier=notifier, backups=backups,
                     crashes=crashes, steam=steam, updater=updater, live=live)
    config = uvicorn.Config(app, host=settings.host,
                            port=settings.port, log_config=None, access_log=False,
                            ws_ping_interval=20,
                            # The peer must be the real socket peer: auth trusts the
                            # Tailscale header only from loopback (basecamp/auth.py).
                            proxy_headers=False)
    web = uvicorn.Server(config)
    web_thread = threading.Thread(target=web.run, name="web", daemon=True)
    web_thread.start()
    log.info("Web page on http://%s:%s", settings.host, settings.port)
    try:
        while not stop.wait(0.5):
            if not web_thread.is_alive():
                log.error("The web server stopped (port %s in use?); Basecamp exits",
                          settings.port)
                break
    except KeyboardInterrupt:
        stop.set()
    finally:
        web.should_exit = True
        web_thread.join(10)
        # Game servers keep running (D-09); the next start adopts them.
        log.info("Basecamp stopping; game servers are left running")
        backups.stop_scheduler()
        updater.close()
        supervisor.remote.close()
        steam.close()
        live.close()
        notifier.close()
        supervisor.detach()
        store.close()
    return updater.finish_exit()


def first_start_since_boot(settings: Settings) -> bool:
    """True the first time Basecamp starts after the PC booted (R-60), remembered in
    ``<data>\\boot.json``."""
    path = settings.data_dir / "boot.json"
    boot = psutil.boot_time()
    try:
        last = float(json.loads(path.read_text(encoding="utf-8"))["boot_time"])
    except (OSError, ValueError, KeyError, TypeError):
        last = None
    if last is not None and abs(last - boot) < 5:  # boot_time wobbles by a second or so
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"boot_time": boot}), encoding="utf-8")
    return True
