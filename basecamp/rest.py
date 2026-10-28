"""Palworld's REST API, standard library only (D-95).

Palworld marks its RCON as deprecated and recommends this API instead. It is turned on with
``RESTAPIEnabled=True`` in PalWorldSettings.ini, listens on ``RESTAPIPort`` (8212) and
takes HTTP basic auth with the user ``admin`` and the server's ``AdminPassword``.

Stop steps, warnings and the console use it with a command line like the other channels,
so templates stay plain text:

    announce <message>          POST /v1/api/announce {"message"}
    save                        POST /v1/api/save
    shutdown <seconds> [text]   POST /v1/api/shutdown {"waittime", "message"}
    stop                        POST /v1/api/stop (no save; not for stop recipes)
    info | players | metrics | settings     GET /v1/api/<name>

The password is never logged or put in an error message.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from basecamp.rcon import check_host

log = logging.getLogger(__name__)

USER = "admin"
_GETS = ("info", "players", "metrics", "settings")
_POSTS = ("announce", "save", "shutdown", "stop")


class RestError(Exception):
    pass


def parse(command: str) -> tuple[str, str, dict | None]:
    """``"shutdown 10 Bye"`` -> ("POST", "shutdown", {"waittime": 10, "message": "Bye"}).
    Raises RestError for a command the API doesn't have."""
    name, _, rest = command.strip().partition(" ")
    name = name.lower()
    rest = rest.strip()
    if name in _GETS:
        return "GET", name, None
    if name == "announce":
        if not rest:
            raise RestError("announce needs a message")
        return "POST", name, {"message": rest}
    if name == "shutdown":
        secs, _, message = rest.partition(" ")
        try:
            wait = int(secs) if secs else 1
        except ValueError:
            raise RestError("shutdown takes the seconds to wait first, e.g. "
                            "'shutdown 10 Server is shutting down'") from None
        return "POST", name, {"waittime": max(wait, 1), "message": message.strip()}
    if name in ("save", "stop"):
        return "POST", name, {}
    raise RestError(f"Unknown REST command '{name}'. Commands: "
                    + ", ".join(sorted(_GETS + _POSTS)))


class RestClient:
    def __init__(self, host: str, port: int, password: str, *, user: str = USER,
                 timeout: float = 10.0, allow_remote: bool = False):
        check_host(host, allow_remote)
        self.base = f"http://{_url_host(host)}:{port}/v1/api/"
        self.timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def __repr__(self) -> str:  # never the password
        return f"RestClient({self.base})"

    def command(self, text: str) -> str:
        """Run a command line (see the module doc) and return the answer as text."""
        method, name, body = parse(text)
        return self.call(method, name, body)

    def call(self, method: str, name: str, body: dict | None = None) -> str:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + name, data=data, method=method,
                                     headers={"Authorization": self._auth,
                                              "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(1 << 20).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RestError("the REST API refused the password") from None
            raise RestError(f"the REST API answered {e.code} {e.reason}") from None
        except TimeoutError:
            raise RestError("the REST API didn't answer in time") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise RestError("the REST API didn't answer in time") from None
            if isinstance(e.reason, ConnectionRefusedError):
                raise RestError("the REST API isn't listening (RESTAPIEnabled=True in "
                                "PalWorldSettings.ini?)") from None
            raise RestError(f"REST connection failed: {e.reason}") from None
        except (OSError, ValueError) as e:  # reset mid-answer, bad status line
            raise RestError(f"REST connection failed: {e}") from None
        return raw.strip()


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host
