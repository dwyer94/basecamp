"""Player counts (batch 2, item 8; D-132): Palworld's REST API, an RCON command and pattern,
Steam's server query, against tools/fake_rest.py, fake_rcon.py and fake_a2s.py."""

from __future__ import annotations

import dataclasses
import sys

import pytest
from fastapi.testclient import TestClient

from basecamp import a2s, remote
from basecamp.api import views
from basecamp.api.app import create_app
from basecamp.models import PlayerQuery, PlayerSource
from basecamp.players import Players, read_rcon, read_rest
from basecamp.supervisor import Supervisor
from tests.conftest import REPO, fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_FRIEND, BASE

sys.path.insert(0, str(REPO / "tools"))
from fake_a2s import FakeA2S
from fake_rcon import FakeRcon
from fake_rest import FakeRest


def test_a2s_with_and_without_a_challenge():
    for challenge in (True, False):
        with FakeA2S(players=3, most=16, challenge=challenge) as fake:
            assert a2s.query_info("127.0.0.1", fake.port, 2) == (3, 16)
            assert fake.requests == (2 if challenge else 1)
    with FakeA2S() as fake:
        fake.silent = True
        with pytest.raises(a2s.A2SError, match="no answer"):
            a2s.query_info("127.0.0.1", fake.port, 0.3)
    with pytest.raises(a2s.A2SError, match="cut short"):
        a2s.parse_info(b"\xff\xff\xff\xffI\x11name\x00map")


def test_reading_rcon_and_rest_answers():
    assert read_rcon("Players: 3/10\n", r"Players: (\d+)/(\d+)") == (3, 10)
    assert read_rcon("Players: 3\n", r"Players: (\d+)") == (3, None)
    listing = "Name,ID\nAnna,1\nBo,2\n"
    assert read_rcon(listing, r"^\w+,\d+$") == (2, None)
    assert read_rcon("", r"^\w+,\d+$") == (0, None)
    with pytest.raises(ValueError, match="didn't match"):
        read_rcon("nobody", r"Players: (\d+)")
    assert read_rest('{"currentplayernum": 2, "maxplayernum": 32}') == (2, 32)
    with pytest.raises(ValueError):
        read_rest('{"players": []}')
    with pytest.raises(ValueError, match="command and a pattern"):
        PlayerQuery(source=PlayerSource.RCON, command="list")
    with pytest.raises(ValueError, match="regex"):
        PlayerQuery(source=PlayerSource.RCON, command="list", pattern="(")
    with pytest.raises(ValueError, match="query port"):
        PlayerQuery(source=PlayerSource.A2S)


def running(sup, store, name, **fields):
    server = store.add_server(fake_config(name, "--tick", "0.3", **fields))
    sup.start(server.id)
    wait_for(lambda: sup.state(server.id).value == "running", 20)
    return server


def test_counts_from_each_source_and_unknown(store, settings, sup):
    players = Players(store, sup, settings)
    with FakeRest() as rest, FakeRcon() as rcon, FakeA2S(players=5, most=8) as query:
        by_rest = running(sup, store, "pal", rest_port=rest.port,
                          players=PlayerQuery(source=PlayerSource.REST))
        by_rcon = running(sup, store, "rc", rcon_port=rcon.port, players=PlayerQuery(
            source=PlayerSource.RCON, command="echo Players: 3/10",
            pattern=r"Players: (\d+)/(\d+)"))
        by_a2s = running(sup, store, "q", players=PlayerQuery(source=PlayerSource.A2S,
                                                               port=query.port))
        none = running(sup, store, "none")
        remote.set_password(settings, "rest", by_rest.id, "test")
        remote.set_password(settings, "rcon", by_rcon.id, "test")

        players.poll_once()
        assert players.get(by_rest).count == 0 and players.get(by_rest).most == 16
        assert (players.get(by_rcon).count, players.get(by_rcon).most) == (3, 10)
        assert (players.get(by_a2s).count, players.get(by_a2s).most) == (5, 8)
        assert players.get(none) is None  # the game doesn't report players

        rest.players = [{"name": "Anna"}, {"name": "Bo"}]
        query.players = 6
        players.poll_once()
        assert players.get(by_rest).count == 2 and players.get(by_a2s).count == 6

        remote.set_password(settings, "rest", by_rest.id, "wrong")
        query.silent = True
        players.poll_once()
        unknown = players.get(by_rest)
        assert unknown.count is None and "password" in unknown.error
        assert players.get(by_a2s).count is None
        assert players.friend_view(by_rest) == {"count": None, "max": None}

        sup.stop(by_a2s.id)
        assert players.get(by_a2s) is None  # stopped: no count at all
        players.poll_once()
        assert by_a2s.id not in players._counts


def test_friends_get_the_count_and_nothing_else(store, settings):
    web = dataclasses.replace(settings, admin_logins=(ADMIN,), join_host="<tailscale-ip>")
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    try:
        with FakeA2S(players=2, most=4) as query, \
                TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            server = running(sup, store, "q", players=PlayerQuery(source=PlayerSource.A2S,
                                                                   port=query.port))
            assert c.get("/api/servers", headers=AS_FRIEND).json()[0]["players"] == {
                "count": None, "max": None}  # not asked yet: unknown
            app.state.players.poll_once()
            friend = c.get(f"/api/servers/{server.id}", headers=AS_FRIEND).json()
            assert tuple(friend) == views.FRIEND_DETAIL_FIELDS
            assert friend["players"] == {"count": 2, "max": 4}
            admin = c.get(f"/api/servers/{server.id}").json()
            assert admin["players_detail"]["source"] == "a2s"
            assert admin["config"]["players"]["port"] == query.port
            sup.stop(server.id)
            assert c.get("/api/servers", headers=AS_FRIEND).json()[0]["players"] is None
    finally:
        stop_everything(sup, store)


def test_players_setting_round_trips_through_the_db(store):
    q = PlayerQuery(source=PlayerSource.RCON, command="list", pattern=r"(\d+) players")
    server = store.add_server(fake_config("x", players=q))
    assert store.get_server(server.id).players == q
    other = store.add_server(fake_config("y"))
    assert store.get_server(other.id).players is None
