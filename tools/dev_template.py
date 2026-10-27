"""Write a "Fake server" game template into a data folder, for trying Basecamp on the dev PC.

    .venv\\Scripts\\python tools\\dev_template.py            (data folder: .\\data)
    .venv\\Scripts\\python tools\\dev_template.py D:\\other-data

The template runs tools/fake_server.py with this PC's Python, so it holds absolute paths
and lives in the data folder (gitignored), not in templates/. It is also an example of a
data-folder template (docs/08-templates.md).
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The base interpreter, not a venv's python.exe launcher (see tests/conftest.py).
PYTHON = Path(getattr(sys, "_base_executable", sys.executable))


def main() -> None:
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "data"
    target = data / "templates" / "fake-server.json"
    template = {
        "id": "fake-server",
        "name": "Fake server (dev)",
        "kind": "custom",
        "exe": str(PYTHON),
        "args": f'-I "{REPO / "tools" / "fake_server.py"}" --name Fake --port 27777 '
                "--tick 5 --save-delay 1",
        "working_dir": str(REPO / "tools"),
        "ports": [{"port": 27777, "protocol": "udp", "label": "game"}],
        "join_port_label": "game",
        "ready_pattern": "Server ready",
        "ready_after_s": 15,
        "stop_recipe": [
            {"action": "stdin", "command": "save", "wait_s": 3},
            {"action": "stdin", "command": "quit", "wait_s": 10},
            {"action": "ctrl_break", "wait_s": 10},
        ],
        "notes": "Commands: save, quit, say <text>, crash, hang.",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
