import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import HTTPConnection

from nexus.access import EntryAccessMiddleware, entry_security
from nexus.network import entry_urls


class EntryDiscoveryTests(unittest.TestCase):
    def test_discovers_local_tailnet_proxy_and_reverse_tunnel_entries(self):
        addresses = {
            "lan": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.20")],
            "vpn": [SimpleNamespace(family=socket.AF_INET6, address="fd00::20")],
        }
        tailnet = {
            "Self": {
                "DNSName": "nexus.example.ts.net.",
                "HostName": "nexus",
                "TailscaleIPs": ["100.64.0.20"],
            }
        }
        serve = {
            "Web": {
                "nexus.example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}
            },
            "TCP": {"8443": {"HTTPS": True}},
        }
        with (
            patch("nexus.network.psutil.net_if_addrs", return_value=addresses),
            patch("nexus.network.socket.gethostname", return_value="nexus"),
            patch("nexus.network._tailscale_json", side_effect=[tailnet, serve]),
        ):
            urls = entry_urls("http://127.0.0.1:8765", ["https://custom.example"])
        self.assertTrue(
            {
                "http://127.0.0.1:8765",
                "http://localhost:8765",
                "http://[::1]:8765",
                "http://127.0.0.1:18765",
                "http://192.168.1.20:8765",
                "http://[fd00::20]:8765",
                "http://100.64.0.20:8765",
                "http://nexus:8765",
                "http://nexus.example.ts.net:8765",
                "https://nexus.example.ts.net",
                "https://nexus.example.ts.net:8443",
                "https://custom.example",
            }.issubset(urls)
        )

    def test_optional_tailscale_can_be_unconfigured(self):
        with (
            patch("nexus.network.psutil.net_if_addrs", return_value={}),
            patch("nexus.network._tailscale_json", side_effect=[{"Self": None}, {}]),
        ):
            self.assertIn("http://127.0.0.1:9000", entry_urls("http://127.0.0.1:9000", []))

    def test_entry_urls_accept_default_ports_and_reject_non_origin_configuration(self):
        security = entry_security({"https://NEXUS.example/", "http://[::1]:8765"})
        self.assertEqual(
            set(security.allowed_hosts), {"nexus.example", "nexus.example:443", "[::1]:8765"}
        )
        self.assertEqual(
            set(security.allowed_origins),
            {
                "https://nexus.example",
                "https://nexus.example:443",
                "http://[::1]:8765",
            },
        )
        for url in (
            "*",
            "https://*.example",
            "ftp://nexus.example",
            "https://nexus.example/path",
            "https://user:password@nexus.example",
            "https://nexus.example?query=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                entry_security({url})


class EntryAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.security = entry_security(
            {
                "http://127.0.0.1:8765",
                "http://127.0.0.1:18765",
                "http://192.168.1.20:8765",
                "https://nexus.example.ts.net",
            }
        )

    async def test_http_and_websocket_apply_one_host_and_origin_policy(self):
        cases = [
            ("192.168.1.20:8765", "http://192.168.1.20:8765", None),
            ("127.0.0.1:18765", None, None),
            ("127.0.0.1:8765", "https://nexus.example.ts.net", None),
            ("nexus.example.ts.net", "https://nexus.example.ts.net", None),
            ("192.168.1.20:8765", "https://evil.example", 403),
            ("192.168.1.20:8765", "null", 403),
            ("192.168.1.20:8765", "", 403),
            ("192.168.1.20:8765", "http://192.168.1.20:8766", 403),
            ("evil.example:8765", "http://evil.example:8765", 421),
            ("192.168.1.20.evil.example:8765", None, 421),
            ("127.0.0.1:9999", None, 421),
        ]
        for protocol in ("http", "websocket"):
            for host, origin, status in cases:
                with self.subTest(protocol=protocol, host=host, origin=origin):
                    app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
                    headers = [(b"host", host.encode())]
                    if origin is not None:
                        headers.append((b"origin", origin.encode()))
                    scope = {"type": protocol, "headers": headers}
                    await EntryAccessMiddleware(app, self.security)(scope, receive, send)
                    if status is None:
                        app.assert_awaited_once_with(scope, receive, send)
                        send.assert_not_awaited()
                    else:
                        app.assert_not_awaited()
                        first = send.await_args_list[0].args[0]
                        self.assertEqual(
                            first["code"] if protocol == "websocket" else first["status"],
                            1008 if protocol == "websocket" else status,
                        )
                    if origin != "":
                        response = await TransportSecurityMiddleware(
                            self.security
                        ).validate_request(HTTPConnection(scope))
                        self.assertEqual(response.status_code if response else None, status)

    async def test_untrusted_forwarded_headers_do_not_register_an_entry(self):
        app, send = AsyncMock(), AsyncMock()
        scope = {
            "type": "http",
            "headers": [
                (b"host", b"evil.example"),
                (b"x-forwarded-host", b"127.0.0.1:8765"),
                (b"x-forwarded-proto", b"http"),
            ],
        }
        await EntryAccessMiddleware(app, self.security)(scope, AsyncMock(), send)
        app.assert_not_awaited()
        self.assertEqual(send.await_args_list[0].args[0]["status"], 421)
