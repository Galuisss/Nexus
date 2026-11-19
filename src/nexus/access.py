"""Shared Host and Origin policy for HTTP, WebSocket, and MCP entry points."""

from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def entry_security(urls: set[str]) -> TransportSecuritySettings:
    hosts, origins = set(), set()
    for value in urls:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or "*" in url.netloc
            or url.username is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError(f"入口地址须为 HTTP/HTTPS 来源（协议、主机与端口）：{value}")
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        default_port = 443 if url.scheme == "https" else 80
        port = url.port or default_port
        hosts.add(f"{host}:{port}")
        origins.add(f"{url.scheme}://{host}:{port}")
        if port == default_port:
            hosts.add(host)
            origins.add(f"{url.scheme}://{host}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


class EntryAccessMiddleware:
    def __init__(self, app, security: TransportSecuritySettings):
        self.app = app
        self.hosts = set(security.allowed_hosts)
        self.origins = set(security.allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            headers = Headers(scope=scope)
            error, status = None, 0
            if headers.get("host") not in self.hosts:
                error, status = "Invalid Host header", 421
            elif "origin" in headers and headers["origin"] not in self.origins:
                error, status = "Invalid Origin header", 403
            if error:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": error})
                else:
                    await JSONResponse({"error": {"message": error}}, status_code=status)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)
