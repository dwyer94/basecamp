"""App start: settings, logging, migrations, adopt orphans, start-on-boot servers, and the
web server on ``settings.host:settings.port`` (127.0.0.1 only, D-27).
"""

from __future__ import annotations

import logging
import threading

import uvicorn

from basecamp import __version__
from basecamp.api.app import create_app
from basecamp.db import Store
from basecamp.logs import setup_logging
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor

log = logging.getLogger(__name__)


def run(settings: Settings | None = None, stop: threading.Event | None = None) -> None:
    settings = settings or Settings.load()
    stop = stop or threading.Event()
    setup_logging(settings)
    log.info("Basecamp %s starting, data in %s", __version__, settings.data_dir)
    store = Store.open(settings.db_path, settings.migrations_dir)
    supervisor = Supervisor(store, settings)
    adopted = supervisor.adopt_orphans()
    if adopted:
        log.info("Adopted %d running server(s)", len(adopted))
    boot = threading.Thread(target=supervisor.start_on_boot, kwargs={"cancel": stop},
                            name="start on boot", daemon=True)
    boot.start()
    config = uvicorn.Config(create_app(store, supervisor, settings), host=settings.host,
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
        supervisor.detach()
        store.close()
