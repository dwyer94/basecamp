"""App start: settings, logging, migrations, adopt orphans, start-on-boot servers.

Phase 1 has no web server yet; ``run`` just keeps the supervisor alive until stopped.
Phase 2 starts Uvicorn on ``settings.host:settings.port`` where marked below.
"""

from __future__ import annotations

import logging
import threading

from basecamp import __version__
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
    # Phase 2: start the web server here.
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop.set()
    finally:
        # Game servers keep running (D-09); the next start adopts them.
        log.info("Basecamp stopping; game servers are left running")
        supervisor.detach()
        store.close()
