"""Telling the iPhone app where the server is: what gets advertised over
Bonjour, and the address + QR code /profile shows as the fallback."""
import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from epublib import discovery
from epublib.config import load
from tests.conftest import make_cfg


def test_nothing_is_advertised_behind_a_proxy_unless_its_port_is_given(tmp_path):
    assert discovery.plan(make_cfg(tmp_path, host="127.0.0.1")) is None
    behind = discovery.plan(make_cfg(tmp_path, host="127.0.0.1", discovery_advertise_port=80))
    assert behind.port == 80


def test_a_lan_bind_announces_its_own_port(tmp_path):
    assert discovery.plan(make_cfg(tmp_path, host="0.0.0.0", port=8123)).port == 8123


def test_it_can_be_switched_off(tmp_path):
    cfg = make_cfg(tmp_path, host="0.0.0.0", discovery_enabled=False, discovery_advertise_port=80)
    assert discovery.plan(cfg) is None


def test_the_discovery_section_of_the_config_is_read(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('host = "127.0.0.1"\n[discovery]\nenabled = true\nadvertise_port = 80\n')
    cfg = load(f)
    assert cfg.discovery_enabled and cfg.discovery_advertise_port == 80
    f.write_text("[discovery]\nenabled = false\n")
    cfg = load(f)
    assert not cfg.discovery_enabled and cfg.discovery_advertise_port is None
    f.write_text("")
    assert load(f).discovery_enabled


def test_the_service_record_names_the_port_and_a_display_name():
    info = discovery.build_info(discovery.Advert(port=8000, name="nas"), ["192.0.2.10"])
    assert info.type == "_hondana._tcp.local."
    assert info.port == 8000
    assert info.parsed_addresses() == ["192.0.2.10"]
    assert info.properties[b"name"] == b"nas" and info.properties[b"scheme"] == b"http"
    assert info.properties[b"path"] == b"/"


def test_the_advert_carries_a_host_only_when_one_is_configured():
    plain = discovery.build_info(discovery.Advert(port=80, name="nas"), ["192.0.2.10"])
    assert b"host" not in plain.properties
    named = discovery.build_info(discovery.Advert(port=80, name="nas", host="books.local"), ["192.0.2.10"])
    assert named.properties[b"host"] == b"books.local"


def test_an_advert_resolves_to_the_url_the_phone_would_use():
    props = {"scheme": "http", "path": "/", "name": "nas"}
    assert discovery.advert_url(props, ["192.0.2.10"], 8000) == "http://192.0.2.10:8000"
    # Behind a proxy: its own name, and no :80.
    assert discovery.advert_url({**props, "host": "books.local"}, ["192.0.2.10"], 80) == "http://books.local"
    assert discovery.advert_url(props, [], 8000) is None


def test_plan_passes_the_configured_host_on(tmp_path):
    cfg = make_cfg(tmp_path, host="127.0.0.1", discovery_advertise_port=80,
                   discovery_advertise_host="books.local")
    assert discovery.plan(cfg).host == "books.local"
    assert discovery.plan(make_cfg(tmp_path, host="0.0.0.0")).host == ""


def test_advertise_host_is_read_from_the_config(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[discovery]\nadvertise_host = " books.local "\n')
    assert load(f).discovery_advertise_host == "books.local"
    f.write_text("")
    assert load(f).discovery_advertise_host == ""


def test_docker_and_bridge_interfaces_are_not_advertised(monkeypatch):
    import ifaddr

    def adapter(name, ip):
        return types.SimpleNamespace(name=name, ips=[types.SimpleNamespace(ip=ip, is_IPv4=True)])

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        adapter("lo", "127.0.0.1"), adapter("eth0", "192.0.2.10"), adapter("docker0", "172.17.0.1"),
        adapter("br-1a2b3c", "172.18.0.1"), adapter("veth9f", "172.19.0.1")])
    assert discovery.local_ipv4() == ["192.0.2.10"]


class FakeZeroconf:
    registered: list = []
    closed = False

    async def async_register_service(self, info):
        FakeZeroconf.registered.append(info)

    async def async_unregister_service(self, info):
        FakeZeroconf.registered.remove(info)

    async def async_close(self):
        FakeZeroconf.closed = True


def run_advertiser(monkeypatch, cfg, *, addresses=("192.0.2.5",), zeroconf=FakeZeroconf):
    FakeZeroconf.registered, FakeZeroconf.closed = [], False
    fake = types.ModuleType("zeroconf.asyncio")
    fake.AsyncZeroconf = zeroconf
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", fake)
    monkeypatch.setattr(discovery, "local_ipv4", lambda: list(addresses))
    adv = discovery.Advertiser()
    started = asyncio.run(adv.start(cfg))
    return adv, started


def test_the_advertiser_registers_and_then_withdraws(monkeypatch, tmp_path):
    adv, started = run_advertiser(monkeypatch, make_cfg(tmp_path, host="0.0.0.0"))
    assert started and len(FakeZeroconf.registered) == 1
    asyncio.run(adv.stop())
    assert FakeZeroconf.registered == [] and FakeZeroconf.closed


def test_a_failure_to_register_never_stops_the_server(monkeypatch, tmp_path):
    class Broken(FakeZeroconf):
        async def async_register_service(self, info):
            raise OSError("no network")

    adv, started = run_advertiser(monkeypatch, make_cfg(tmp_path, host="0.0.0.0"), zeroconf=Broken)
    assert not started and FakeZeroconf.closed
    asyncio.run(adv.stop())  # stopping what never started is fine


def test_nothing_starts_without_a_lan_address(monkeypatch, tmp_path):
    _, started = run_advertiser(monkeypatch, make_cfg(tmp_path, host="0.0.0.0"), addresses=())
    assert not started and FakeZeroconf.registered == []


def test_a_loopback_host_is_swapped_for_the_lan_address(monkeypatch):
    monkeypatch.setattr(discovery, "local_ipv4", lambda: ["192.168.1.7"])
    assert discovery.connect_url("http", "localhost:8000") == ("http://192.168.1.7:8000", True)
    assert discovery.connect_url("http", "127.0.0.1") == ("http://192.168.1.7", True)
    assert discovery.connect_url("http", "books.local") == ("http://books.local", False)
    assert discovery.connect_url("http", "192.0.2.10:8000") == ("http://192.0.2.10:8000", False)
    monkeypatch.setattr(discovery, "local_ipv4", lambda: [])
    assert discovery.connect_url("http", "localhost:8000") == ("http://localhost:8000", False)


def test_the_connect_link_carries_the_server_and_profile():
    link = discovery.connect_link("http://books.local", "alice")
    assert link == "hondana://connect?server=http%3A%2F%2Fbooks.local&profile=alice"


@pytest.fixture
def client(server):
    return TestClient(server.app)


def test_the_connect_endpoint_uses_the_hosts_own_name(server, client):
    r = client.get("/api/connect", headers={"host": "books.local"})
    assert r.status_code == 200
    body = r.json()
    slug = server.profiles_mod.list_profiles(server.cfg)["active"]
    assert body["url"] == "http://books.local" and not body["lan_substituted"]
    assert body["profile"] == slug
    assert body["link"] == discovery.connect_link("http://books.local", slug)


def test_the_qr_code_is_an_svg_of_that_link(client):
    r = client.get("/api/connect/qr.svg", headers={"host": "books.local"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<svg")
