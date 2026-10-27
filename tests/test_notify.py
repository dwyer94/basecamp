"""Discord notifier (R-60..R-62): secrets.toml, per-server events, retries, boot message."""

from __future__ import annotations

import dataclasses
import json
import tomllib

import pytest

from basecamp import main, notify
from basecamp.models import NotifyEvent, ServerState
from basecamp.notify import Notifier
from tests.conftest import fake_config, wait_for

FAKE_URL = "https://discord.com/api/webhooks/123/abcdefTOKEN"


@pytest.fixture
def fast(settings):
    return dataclasses.replace(settings, notify_retry_s=(0.1, 0.1))


def test_webhooks_live_in_secrets_toml_and_other_secrets_are_kept(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.secrets_path.write_text(
        '[rcon]\npalworld = "pw \\"quoted\\""\n\n[other]\nn = 3\nflag = true\n'
        'list = ["a", "b"]\n', encoding="utf-8")

    notify.set_webhook(settings, FAKE_URL)
    notify.set_webhook(settings, FAKE_URL + "2", server_id=7)

    data = tomllib.loads(settings.secrets_path.read_text(encoding="utf-8"))
    assert data["rcon"] == {"palworld": 'pw "quoted"'}
    assert data["other"] == {"n": 3, "flag": True, "list": ["a", "b"]}
    assert notify.webhooks(settings) == (FAKE_URL, {7: FAKE_URL + "2"})

    notify.set_webhook(settings, "", server_id=7)
    notify.set_webhook(settings, "")
    assert notify.webhooks(settings) == ("", {})
    assert "discord" not in tomllib.loads(settings.secrets_path.read_text(encoding="utf-8"))


def test_mask_hides_the_token():
    masked = notify.mask(FAKE_URL)
    assert "abcdefTOKEN" not in masked and masked.startswith("https://discord.com/api/webhooks/123/")


def test_page_only_accepts_discord_webhook_urls():
    assert notify.WEBHOOK_URL.match(FAKE_URL)
    assert notify.WEBHOOK_URL.match("https://discordapp.com/api/webhooks/1/x-y_z")
    for bad in ("http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/x",
                "https://discord.com.evil.example/api/webhooks/1/x", "https://discord.com/x"):
        assert not notify.WEBHOOK_URL.match(bad)


def test_events_post_unless_turned_off_and_server_webhook_wins(store, fast, webhook):
    notify.set_webhook(fast, webhook.url)
    n = Notifier(fast, store)
    on = store.add_server(fake_config("alpha"))
    quiet = store.add_server(fake_config("bravo", notify_off=[NotifyEvent.CRASHED]))

    assert n.server_event(on, NotifyEvent.CRASHED, "alpha crashed")
    assert not n.server_event(quiet, NotifyEvent.CRASHED, "bravo crashed")
    assert n.server_event(quiet, NotifyEvent.UP, "bravo up")
    wait_for(lambda: len(webhook.posts) == 2)
    assert webhook.texts() == ["alpha crashed", "bravo up"]
    assert webhook.posts[0]["allowed_mentions"] == {"parse": []}  # no @everyone pings

    notify.set_webhook(fast, webhook.url.replace("/1/", "/2/"), server_id=quiet.id)
    assert n.url_for(quiet.id).endswith("/2/test") and n.url_for(on.id) == webhook.url
    n.close()


def test_no_webhook_means_nothing_is_sent(store, settings):
    n = Notifier(settings, store)
    assert not n.post("hello")
    assert n.pending() == 0


def test_failures_are_retried_and_never_block(store, fast, webhook):
    notify.set_webhook(fast, webhook.url)
    webhook.answers = [(500, None), (429, {"retry_after": 0.2})]
    n = Notifier(fast, store)

    n.post("first")
    n.post("second")  # queued behind; posting returns at once either way

    wait_for(lambda: n.sent == 2, timeout=5)
    assert sorted(webhook.texts()) == ["first", "second"]
    assert n.failed == 0
    n.close()


def test_bad_webhook_is_not_retried(store, fast, webhook):
    notify.set_webhook(fast, webhook.url)
    webhook.answers = [(404, {"message": "Unknown Webhook"})]
    n = Notifier(fast, store)
    n.post("lost")
    wait_for(lambda: n.failed == 1)
    assert n.sent == 0 and n.pending() == 0
    n.close()


def test_unreachable_webhook_gives_up_after_the_retries(store, fast):
    notify.set_webhook(fast, "http://127.0.0.1:9/api/webhooks/1/x")  # discard port: refused
    n = Notifier(fast, store)
    n.post("nobody home")
    wait_for(lambda: n.failed == 1, timeout=10)
    n.close()


def test_up_and_stopped_from_state_changes(store, sup, fast, webhook):
    fast = dataclasses.replace(fast, join_host="<tailscale-ip>")
    notify.set_webhook(fast, webhook.url)
    n = Notifier(fast, store)
    sup.add_listener(n.on_state)
    server = store.add_server(fake_config("charlie"))

    sup.start(server.id)
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
    sup.stop(server.id)

    wait_for(lambda: len(webhook.posts) == 2)
    up, stopped = webhook.texts()
    assert "charlie" in up and f"<tailscale-ip>:{server.join_port}" in up
    assert "stopped" in stopped
    n.close()


def test_boot_message_once_per_pc_boot(settings, monkeypatch):
    monkeypatch.setattr(main.psutil, "boot_time", lambda: 1_000_000.0)
    assert main.first_start_since_boot(settings)
    assert not main.first_start_since_boot(settings)
    monkeypatch.setattr(main.psutil, "boot_time", lambda: 1_000_001.5)  # same boot, rounding
    assert not main.first_start_since_boot(settings)
    monkeypatch.setattr(main.psutil, "boot_time", lambda: 2_000_000.0)
    assert main.first_start_since_boot(settings)
    assert json.loads((settings.data_dir / "boot.json").read_text())["boot_time"] == 2_000_000.0
