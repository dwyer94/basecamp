"""Plan A identity (D-27, D-28): who is admin, and when the Tailscale header is trusted."""

from __future__ import annotations

import pytest

from basecamp import auth
from basecamp.settings import Settings

ADMIN = "owner@example.com"


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings.load(tmp_path, admin_logins=[ADMIN])


def who(cfg, peer="127.0.0.1", **headers):
    lower = {k.replace("_", "-").lower(): v for k, v in headers.items()}
    lower.setdefault("host", "127.0.0.1:8150")
    return auth.identify(peer, lower, cfg)


def test_the_pc_itself_is_the_local_admin(cfg):
    for host in ("127.0.0.1:8150", "localhost:8150", "[::1]:8150"):
        w = who(cfg, host=host)
        assert w.is_admin and w.login == auth.LOCAL_ADMIN
    assert who(cfg, peer="::1").is_admin


def test_admin_login_through_tailscale_serve(cfg):
    w = who(cfg, tailscale_user_login="Owner@Example.com", x_forwarded_for="100.64.0.2",
            host="box.example.ts.net")
    assert w.is_admin and w.login == "Owner@Example.com"


def test_any_other_login_is_a_friend(cfg):
    w = who(cfg, tailscale_user_login="pal@example.com", x_forwarded_for="100.64.0.3",
            host="box.example.ts.net")
    assert not w.is_admin and w.login == "pal@example.com"


def test_header_from_a_non_loopback_peer_is_ignored(cfg):
    # Basecamp exposed some other way (wrong bind, a port forward): anyone could send it.
    w = who(cfg, peer="100.64.0.9", tailscale_user_login=ADMIN)
    assert not w.is_admin and w.login == ""
    assert not who(cfg, peer="192.0.2.10").is_admin  # and no header isn't local either


def test_proxied_request_without_login_is_a_friend(cfg):
    # Serve sends no login for tagged devices or Funnel; it still connects from loopback.
    assert not who(cfg, x_forwarded_for="100.64.0.4", host="box.example.ts.net").is_admin
    assert not who(cfg, x_forwarded_for="100.64.0.4").is_admin
    assert not who(cfg, host="box.example.ts.net").is_admin
    assert not who(cfg, forwarded="for=100.64.0.4").is_admin


def test_empty_login_header_is_no_login(cfg):
    assert not who(cfg, tailscale_user_login="  ", host="box.example.ts.net").is_admin


def test_no_admins_configured_means_only_the_pc_is_admin(tmp_path):
    cfg = Settings.load(tmp_path)
    assert not who(cfg, tailscale_user_login=ADMIN).is_admin
    assert who(cfg).is_admin


def test_admin_logins_from_settings_toml(tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[basecamp]\nadmin_logins = ["a@example.com", "b@example.com"]\n'
        'join_host = "<tailscale-ip>"\n')
    cfg = Settings.load(tmp_path)
    assert cfg.admin_logins == ("a@example.com", "b@example.com")
    assert cfg.join_host == "<tailscale-ip>"


def test_host_names_we_answer_to():
    assert auth.host_allowed("127.0.0.1:8150")
    assert auth.host_allowed("localhost:8150")
    assert auth.host_allowed("[::1]:8150")
    assert auth.host_allowed("box.example.ts.net")
    # DNS rebinding: another site's name pointed at 127.0.0.1.
    assert not auth.host_allowed("evil.example.com")
    assert not auth.host_allowed("ts.net.evil.example.com")
    assert not auth.host_allowed("")
    assert not auth.host_allowed(None)


def test_same_origin_check():
    assert auth.check_origin({"host": "127.0.0.1:8150"})  # curl, scripts: no Origin
    assert auth.check_origin({"host": "127.0.0.1:8150", "origin": "http://127.0.0.1:8150"})
    assert auth.check_origin({"host": "box.example.ts.net",
                              "origin": "https://box.example.ts.net"})
    assert not auth.check_origin({"host": "127.0.0.1:8150", "origin": "https://evil.example"})
    assert not auth.check_origin({"host": "127.0.0.1:8150", "origin": "null"})
