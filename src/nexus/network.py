"""Local addresses and optional Tailscale integration."""

import json
import socket
import subprocess
from urllib.parse import urlsplit

import psutil

REVERSE_PORT = 18765


def tailscale_ip() -> str:
    return subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip()


def default_url() -> str:
    return "http://127.0.0.1:8765"


def local_url(host: str | None, port: int) -> str:
    address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host) or "127.0.0.1"
    if ":" in address:
        address = f"[{address}]"
    return f"http://{address}:{port}"


def _tailscale_json(*arguments: str) -> dict:
    try:
        return json.loads(
            subprocess.check_output(
                ["tailscale", *arguments, "--json"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        )
    except (OSError, subprocess.SubprocessError):
        return {}


def https_url(port: int) -> str | None:
    config = _tailscale_json("serve", "status")
    for address, site in config.get("Web", {}).items():
        target = urlsplit(site.get("Handlers", {}).get("/", {}).get("Proxy", ""))
        public = urlsplit(f"https://{address}")
        if (
            target.hostname in {"127.0.0.1", "localhost", "::1"}
            and target.port == port
            and config.get("TCP", {}).get(str(public.port or 443), {}).get("HTTPS")
        ):
            return f"https://{address.removesuffix(':443')}"
    return None


def entry_urls(nexus_url: str, extra_urls: list[str]) -> set[str]:
    port = urlsplit(nexus_url).port or 80
    loopback = {"127.0.0.1", "localhost", "::1"}
    hosts = loopback | {socket.gethostname()}
    for addresses in psutil.net_if_addrs().values():
        hosts.update(
            address.address
            for address in addresses
            if address.family in {socket.AF_INET, socket.AF_INET6}
        )
    tailnet = _tailscale_json("status").get("Self") or {}
    hosts.update(tailnet.get("TailscaleIPs", []))
    hosts.update(tailnet[key].rstrip(".") for key in ("DNSName", "HostName") if tailnet.get(key))
    urls = {local_url(host, port) for host in hosts}
    urls.update(local_url(host, REVERSE_PORT) for host in loopback)
    urls.update([nexus_url, *extra_urls])
    if name := tailnet.get("DNSName"):
        urls.add(f"https://{name.rstrip('.')}")
    if public := https_url(port):
        urls.add(public)
    return urls
