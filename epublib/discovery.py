"""Advertise the server on the LAN over Bonjour/mDNS so Hondana for iPhone can
find it without anyone typing an address.

The service is `_hondana._tcp` on the configured port; the TXT record carries the
scheme, the path, a display name and, when a proxy only answers to one Host,
that `host`. `Advertiser` runs inside the FastAPI
lifespan and never lets a failure (no network, a name clash) stop the server.
The `connect` helpers back `GET /api/connect`: the address a phone should type
and the `hondana://connect` link its camera can open from a QR code.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import Config

SERVICE_TYPE = "_hondana._tcp.local."
LOOPBACK_HOSTS = {"localhost", "::1"}
# Container/VM bridges, tunnels and VPNs: addresses a phone on the LAN can't reach.
VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "cali", "lxc", "podman",
                          "bridge", "vmnet", "utun", "tailscale", "wg", "zt", "tun", "tap", "ipsec")
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and carrier NAT, never the home LAN
HOME_LANS = tuple(ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"))
DOCKER_NETS = ipaddress.ip_network("172.16.0.0/12")  # where Docker puts a container's bridge address
CONTAINER_HINT = ("running in a container whose only addresses are its own bridge network, which "
                  "phones can't reach: use host networking, or set discovery.advertise_addresses "
                  "and public_url to this machine's LAN address")


def in_container() -> bool:
    flag = os.environ.get("HONDANA_IN_CONTAINER", "").strip().lower()
    return flag in ("1", "true", "yes") or Path("/.dockerenv").exists()


def is_loopback(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def display_name(cfg: Config | None = None) -> str:
    """What the phone lists: the configured name, else the machine's name without
    `.local` (in a container that is a random id, so "Hondana")."""
    if cfg is not None and cfg.discovery_name:
        return cfg.discovery_name
    if in_container():
        return "Hondana"
    name = socket.gethostname().split(".")[0]
    return name or "Hondana"


def is_virtual_iface(name: str) -> bool:
    return (name or "").lower().startswith(VIRTUAL_IFACE_PREFIXES)


def local_ipv4() -> list[str]:
    """This machine's usable IPv4 addresses (not loopback, not link-local, not on a
    docker/bridge/VPN interface a phone can't reach), home-LAN ranges first."""
    import ifaddr  # a zeroconf dependency, imported lazily like the other heavy ones

    found: list[str] = []
    for adapter in ifaddr.get_adapters():
        if is_virtual_iface(adapter.name):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            addr = ipaddress.ip_address(ip.ip)
            if addr.is_loopback or addr.is_link_local or addr in CGNAT or ip.ip in found:
                continue
            found.append(ip.ip)
    return sorted(found, key=lambda a: not any(ipaddress.ip_address(a) in n for n in HOME_LANS))


def lan_addresses(cfg: Config) -> list[str]:
    """The addresses to announce and to put in a QR code: the configured ones, else
    this machine's. In a container on a bridge network the only address is the
    bridge's, which no phone can reach, so there are none."""
    if cfg.discovery_advertise_addresses:
        return list(cfg.discovery_advertise_addresses)
    found = local_ipv4()
    if in_container() and all(ipaddress.ip_address(a) in DOCKER_NETS for a in found):
        return []
    return found


@dataclass(frozen=True)
class Advert:
    port: int
    name: str
    host: str = ""  # the name the proxy answers to; "" = leave it to the phone to use the advert's own


def plan(cfg: Config) -> Advert | None:
    """What to advertise, or None when nothing should be: switched off, or bound to
    loopback behind a proxy whose port wasn't given (we can't know it)."""
    if not cfg.discovery_enabled:
        return None
    port = cfg.discovery_advertise_port
    if port is None:
        if is_loopback(cfg.host):
            return None
        port = cfg.port
    return Advert(port=port, name=display_name(cfg), host=cfg.discovery_advertise_host)


LABEL_MAX = 63  # bytes in one DNS label; zeroconf refuses a longer service or host name


def fit_label(text: str, room: int) -> str:
    """`text` cut to at most `room` UTF-8 bytes without splitting a character. A
    Japanese computer name is three bytes a character, so twenty of them are too
    long for the service name, and a CI runner's hostname is a UUID."""
    return text.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(" .")


def build_info(advert: Advert, addresses: list[str]):
    from zeroconf import ServiceInfo

    properties = {"scheme": "http", "path": "/", "name": advert.name, "v": "1"}
    if advert.host:  # behind a proxy that only answers to its own Host
        properties["host"] = advert.host
    prefix, suffix = "Hondana on ", "-hondana"
    return ServiceInfo(
        SERVICE_TYPE,
        f"{prefix}{fit_label(advert.name, LABEL_MAX - len(prefix))}.{SERVICE_TYPE}",
        port=advert.port,
        # A server name of our own, so it can't clash with the host's own `<name>.local`.
        server=f"{fit_label(advert.name, LABEL_MAX - len(suffix))}{suffix}.local.",
        properties=properties,
        addresses=[socket.inet_aton(a) for a in addresses],
    )


def advert_url(properties: dict[str, str], addresses: list[str], port: int) -> str | None:
    """The address an advert points at, built the way the phone builds it: the TXT
    `host` when a proxy set one, else the first advertised address."""
    host = properties.get("host") or (addresses[0] if addresses else "")
    if not host:
        return None
    scheme = properties.get("scheme") or "http"
    default = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port == default else f"{host}:{port}"
    path = (properties.get("path") or "/").rstrip("/")
    return f"{scheme}://{netloc}{path}"


def browse(timeout: float = 2.0) -> list[dict[str, str]]:
    """The Hondana servers advertising on this network within `timeout` seconds,
    as `{name, url}`. Used by `cli.py align-remote` when no server is named."""
    import time

    from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

    names: list[str] = []

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added and name not in names:
            names.append(name)

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, handlers=[on_change])
        time.sleep(timeout)
        found = []
        for name in names:
            info = zc.get_service_info(SERVICE_TYPE, name, timeout=1000)
            if info is None:
                continue
            props = {k.decode(): v.decode() for k, v in info.properties.items() if v is not None}
            url = advert_url(props, info.parsed_addresses(IPVersion.V4Only), info.port)
            if url:
                found.append({"name": props.get("name") or name.split(".")[0], "url": url})
        return found
    finally:
        zc.close()


class Advertiser:
    """Register the service on start, withdraw it on stop. `start` returns whether
    anything is being advertised; it logs and returns False rather than raising."""

    def __init__(self) -> None:
        self._zc = None
        self._info = None

    async def start(self, cfg: Config) -> bool:
        advert = plan(cfg)
        if advert is None:
            return False
        try:
            from zeroconf.asyncio import AsyncZeroconf

            addresses = lan_addresses(cfg)
            if not addresses:
                why = CONTAINER_HINT if in_container() else "no LAN address found"
                print(f"discovery: {why}; not advertising")
                return False
            self._info = build_info(advert, addresses)
            self._zc = AsyncZeroconf()
            await self._zc.async_register_service(self._info)
        except Exception as e:  # no network, a name clash, a missing socket: never fatal
            print(f"discovery: not advertising ({type(e).__name__}: {e})")
            await self.stop()
            return False
        print(f"discovery: advertising {self._info.name} on port {advert.port}")
        return True

    async def stop(self) -> None:
        zc, info, self._zc, self._info = self._zc, self._info, None, None
        if zc is None:
            return
        # Each step on its own: a service that never registered must not stop the close.
        for step in ((zc.async_unregister_service(info),) if info is not None else ()) + (zc.async_close(),):
            try:
                await step
            except Exception:
                pass


def connect_url(scheme: str, host_header: str, cfg: Config | None = None) -> tuple[str, bool]:
    """The address a phone should use: `public_url` when one is configured, else the
    request's own `Host` (right for this network). A loopback host (opened as
    localhost:8000 on the server itself) means nothing to a phone, so it is swapped
    for this machine's LAN address; the second value says whether that happened."""
    if cfg is not None and cfg.public_url:
        return cfg.public_url.rstrip("/"), False
    parts = urllib.parse.urlsplit("//" + host_header)
    if parts.hostname and is_loopback(parts.hostname):
        addrs = lan_addresses(cfg) if cfg is not None else local_ipv4()
        if addrs:
            return f"{scheme}://{addrs[0]}" + (f":{parts.port}" if parts.port else ""), True
    return f"{scheme}://{host_header}", False


def connect_hint(url: str, cfg: Config) -> str:
    """Why `url` won't work from a phone, when we can tell; "" otherwise."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if cfg.public_url:
        return ""
    if is_loopback(host):
        if in_container():
            return ("Hondana runs in a container and can't see your network's address. Open this "
                    "page with this computer's network address or name, or set public_url.")
        return "This computer has no network address a phone could use."
    try:
        if in_container() and ipaddress.ip_address(host) in DOCKER_NETS:
            return "That is the container's own address, which phones can't reach. Set public_url."
    except ValueError:
        pass
    return ""


def connect_link(server_url: str, profile: str) -> str:
    """What the QR code encodes: the app's `connect` link."""
    return "hondana://connect?" + urllib.parse.urlencode({"server": server_url, "profile": profile})


def qr_svg(text: str) -> bytes:
    """`text` as an SVG QR code (dark on white, with its quiet zone)."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=8, border=2, dark="#111111", light="#ffffff",
                                     xmldecl=False, nl=False)
    return buf.getvalue()
