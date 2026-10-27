"""The live status message in Discord (R-63, D-63), against a local fake webhook."""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from basecamp import notify
from basecamp.api.app import create_app
from basecamp.livestatus import LiveStatus
from basecamp.models import ServerState, StopAction, StopWarning
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import FakeWebhook, fake_config, stop_everything, wait_for
from tests.test_api import BASE
from tests.test_warning import start_running


@pytest.fixture
def on(settings: Settings) -> Settings:
    return dataclasses.replace(settings, discord_live_status=True, live_status_every_s=0.2,
                               notify_retry_s=(0.2,), join_host="100.64.0.9")


@pytest.fixture
def live(on, store, sup, webhook) -> Iterator[LiveStatus]:
    notify.set_webhook(on, webhook.url)
    ls = LiveStatus(on, store, sup)
    yield ls
    ls.close()


def message(webhook: FakeWebhook) -> str:
    with webhook.lock:
        return next(iter(webhook.messages.values()), "")


def test_off_by_default(settings, store, sup, webhook):
    notify.set_webhook(settings, webhook.url)
    store.add_server(fake_config("alpha"))
    ls = LiveStatus(settings, store, sup)
    ls.poke()
    assert not settings.discord_live_status and ls.view()["on"] is False
    assert ls._thread is None and not webhook.messages


def test_posts_once_then_edits_in_place(live, store, sup, webhook):
    server = store.add_server(fake_config("alpha", ready_after_s=1))
    store.add_server(fake_config("bravo"))
    live.poke()
    wait_for(lambda: len(webhook.messages) == 1)
    text = message(webhook)
    assert text.startswith("**Game servers** · updated <t:")
    assert ":black_circle: **alpha** · stopped" in text and "**bravo** · stopped" in text
    assert not webhook.posts  # not an event message

    sup.start(server.id)  # Stopped -> Starting -> Running, each a poke
    wait_for(lambda: f"**alpha** · running → `100.64.0.9:{server.join_port}`"
             in message(webhook), timeout=20)
    assert len(webhook.messages) == 1 and live.posts == 1 and live.edits >= 1
    edits = len(webhook.edits)
    live.poke()  # nothing changed: no edit
    wait_for(lambda: not live._dirty)
    assert len(webhook.edits) == edits
    assert live.view()["has_message"] and live.view()["error"] == ""


def test_a_deleted_message_is_posted_again(live, store, webhook):
    store.add_server(fake_config("alpha"))
    live.poke()
    wait_for(lambda: len(webhook.messages) == 1)
    first = next(iter(webhook.messages))
    webhook.forget(first)
    store.add_server(fake_config("bravo"))
    live.poke()
    wait_for(lambda: len(webhook.messages) == 1 and first not in webhook.messages)
    assert "**bravo**" in message(webhook) and live.posts == 2


def test_failures_dont_block_and_are_retried(live, store, sup, webhook, caplog):
    server = store.add_server(fake_config("alpha", ready_after_s=1))
    webhook.answers = [(500, None), (429, {"retry_after": 0.1})]
    caplog.set_level(logging.INFO)
    live.poke()
    sup.start(server.id)  # not held up by Discord answering 500
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING, 20)
    wait_for(lambda: "running" in message(webhook), timeout=20)
    assert live.view()["error"] == ""
    assert "Live status message not updated (post: HTTP 500)" in caplog.text
    assert "webhooks/1/test" not in caplog.text  # the URL is a secret


def test_a_new_webhook_gets_its_own_message(live, on, store, webhook):
    store.add_server(fake_config("alpha"))
    live.poke()
    wait_for(lambda: len(webhook.messages) == 1)
    other = FakeWebhook()
    try:
        notify.set_webhook(on, other.url)
        store.add_server(fake_config("bravo"))
        live.poke()
        wait_for(lambda: len(other.messages) == 1)
        assert not other.edits and "**bravo**" in message(other)
    finally:
        other.close()


def test_countdown_shows_in_the_message(live, store, sup, webhook):
    warning = StopWarning(action=StopAction.STDIN, command="say {minutes}", minutes=[1])
    server = store.add_server(fake_config("alpha", stop_warning=warning, ready_after_s=1))
    start_running(sup, server.id)
    threading.Thread(target=sup.stop, args=(server.id,), kwargs={"warn": True},
                     daemon=True).start()
    wait_for(lambda: "**alpha** · running, stopping <t:" in message(webhook), timeout=20)
    sup.stop_now(server.id)
    wait_for(lambda: "**alpha** · stopped" in message(webhook), timeout=20)


def test_settings_page_turns_it_on(on, store):
    off = dataclasses.replace(on, discord_live_status=False)
    sup = Supervisor(store, off)
    app = create_app(store, sup, off)
    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            assert c.get("/api/settings").json()["live_status"]["on"] is False
            res = c.put("/api/settings", json={"discord_live_status": True})
            assert res.status_code == 200 and res.json()["changed"] == ["discord_live_status"]
            assert res.json()["live_status"]["on"] is True
            assert "discord_live_status = true" in (off.data_dir / "settings.toml").read_text()
    finally:
        app.state.live.close()
        stop_everything(sup, store)
