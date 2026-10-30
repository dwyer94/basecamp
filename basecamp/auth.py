"""Who is asking: admin or friend (Plan A, D-27; decisions D-28, D-29).

Basecamp listens on 127.0.0.1 only and is published on the tailnet by ``tailscale serve``,
which connects from loopback. For each request from a tailnet user, Serve sets
``Tailscale-User-Login`` and strips any copy the client sent. So:

- Peer isn't loopback: Basecamp was exposed some other way. The header can't be trusted,
  so the request is a friend, whatever it says.
- Loopback with ``Tailscale-User-Login``: that user. Admin if the login is in
  ``admin_logins``, else a friend.
- Loopback with no Tailscale header, no forwarding headers and a loopback ``Host``: a
  browser or tool on the PC itself, the local admin.
- Anything else from loopback (proxied but no login: a tagged device, Funnel, or another
  proxy): a friend.

Anything running on the PC can set these headers, but it could also just omit them and be
the local admin, so spoofing from the PC gains nothing (D-28). Browsers on the PC are kept
out of that by the same-origin checks below: cross-site requests and WebSockets are refused
(``check_origin``), and so are requests for a Host name that isn't ours (DNS rebinding).

"See what friends see" (D-143): an admin whose request carries the ``bc_view=friend``
cookie is served as a friend (``Who.preview``), every route and WebSocket alike, so the
page shows exactly what friends get. The cookie can only take rights away: it is read only
after the request has been found to be an admin's, so it never changes a friend's request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from basecamp.settings import Settings

LOGIN_HEADER = "tailscale-user-login"
NAME_HEADER = "tailscale-user-name"
# Set by tailscale serve (and most proxies): a request carrying one came through a proxy.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded")
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
# Names the page may be reached by: loopback, or the PC's tailnet name (Serve's HTTPS
# address, <machine>.<tailnet>.ts.net). The real name never needs to be in the repo.
_TAILNET_SUFFIX = ".ts.net"
LOCAL_ADMIN = "localhost"
VIEW_COOKIE = "bc_view"  # "friend": the owner is previewing the friend view (D-143)
PREVIEW_WHO = "owner (preview)"  # who a previewing owner's friend actions are audited as


@dataclass(frozen=True)
class Who:
    login: str  # Tailscale login, "localhost", or "" for an unknown friend
    name: str
    is_admin: bool
    preview: bool = False  # an admin previewing the friend view: served as a friend

    @property
    def label(self) -> str:
        """For the audit log and the page header."""
        return self.login or "friend"


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8150
        return host[: host.find("]") + 1]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str | None) -> bool:
    """The Host header names us, not some other site that resolves to 127.0.0.1."""
    if not host:
        return False
    name = _host_name(host)
    return name in _LOOPBACK_HOSTS or name.endswith(_TAILNET_SUFFIX)


def identify(peer: str | None, headers: Mapping[str, str], settings: Settings) -> Who:
    """``headers`` must be case-insensitive (Starlette's are) or use lower-case keys."""
    who = _identify(peer, headers, settings)
    if who.is_admin and previewing(headers):  # only ever takes rights away
        return Who(login=who.login, name=who.name, is_admin=False, preview=True)
    return who


def previewing(headers: Mapping[str, str]) -> bool:
    """The request carries ``bc_view=friend`` (set by ``POST /api/view``)."""
    raw = headers.get("cookie")
    if not raw:
        return False
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return False
    morsel = jar.get(VIEW_COOKIE)
    return morsel is not None and morsel.value == "friend"


def _identify(peer: str | None, headers: Mapping[str, str], settings: Settings) -> Who:
    if peer not in _LOOPBACK_PEERS:
        return Who(login="", name="", is_admin=False)
    login = (headers.get(LOGIN_HEADER) or "").strip()
    if login:
        name = (headers.get(NAME_HEADER) or "").strip()
        admins = {a.lower() for a in settings.admin_logins}
        return Who(login=login, name=name, is_admin=login.lower() in admins)
    proxied = any(headers.get(h) for h in _PROXY_HEADERS)
    host = headers.get("host") or ""
    if not proxied and _host_name(host) in _LOOPBACK_HOSTS:
        return Who(login=LOCAL_ADMIN, name="This PC", is_admin=True)
    return Who(login="", name="", is_admin=False)


def can_control(who: Who, settings: Settings) -> bool:
    """May start, stop and restart servers (D-131): admins, and friends when the friend
    permission lets them. "logins" needs the friend's own Tailscale login on the request,
    which only ``tailscale serve`` can set (Q-02: not yet seen with a friend's device).
    An owner previewing the friend view counts as a listed friend (D-143)."""
    if who.is_admin:
        return True
    if settings.friend_control == "anyone":
        return True
    if settings.friend_control == "logins" and who.preview:
        return bool(settings.friend_control_logins)
    if settings.friend_control == "logins" and who.login:
        return who.login.lower() in {x.lower() for x in settings.friend_control_logins}
    return False


def check_origin(headers: Mapping[str, str]) -> bool:
    """For state-changing requests and WebSockets: an ``Origin`` header, when present, must
    be this same site. Browsers always send it on cross-site POSTs and on WebSockets (which
    CORS doesn't protect), so a page on another site can't drive the local admin's session.
    Requests without it (curl, scripts on the PC) are allowed: they are local anyway."""
    origin = headers.get("origin")
    if origin is None:
        return True
    host = headers.get("host") or ""
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc.lower() == host.strip().lower()
