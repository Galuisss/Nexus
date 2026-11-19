"""Command-line entry points for the Nexus server and its HTTP clients."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .network import default_url, local_url, tailscale_ip


def _client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get("NEXUS_URL"),
        help="Nexus 服务地址，默认 http://127.0.0.1:8765，也可设置 NEXUS_URL",
    )


def _serve_options(parser: argparse.ArgumentParser) -> None:
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--host", help="监听地址，默认 0.0.0.0（所有本机 IPv4 接口）")
    network.add_argument(
        "--tailscale-only", action="store_true", help="仅监听 Tailscale IPv4 和本机回环"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    parser.add_argument(
        "--allow-url",
        action="append",
        default=[],
        metavar="URL",
        help="额外允许的入口地址，如 https://nexus.example.com，可重复指定",
    )


def _request(url: str, method: str, path: str, **kwargs: Any) -> Any:
    with httpx.Client(base_url=url.rstrip("/"), timeout=60, trust_env=False) as client:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nexus", description="跨机器 Codex 会话控制台")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 界面和 HTTP API")
    _serve_options(serve)
    serve.add_argument("--demo", action="store_true", help="使用独立的交互演示数据")
    serve.add_argument("--demo-config", type=Path, help="使用指定的 JSON 场景配置启动演示")

    service = sub.add_parser("service", help="管理 Linux systemd 用户服务和开机自启")
    actions = service.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="安装服务配置，沿用 serve 的启动参数")
    _serve_options(install)
    actions.add_parser("enable", help="开启开机自启并立即启动，无需登录")
    actions.add_parser("disable", help="关闭开机自启并停止服务")
    actions.add_parser("status", help="查看运行状态和开机自启配置")
    actions.add_parser("restart", help="重启服务，应用更新后的配置")
    logs = actions.add_parser("logs", help="查看服务日志")
    logs.add_argument("--follow", "-f", action="store_true", help="持续跟踪日志")
    logs.add_argument("--lines", "-n", type=int, default=50)
    actions.add_parser("uninstall", help="停止服务并移除服务配置，保留数据和环境配置")

    mcp = sub.add_parser("mcp", help="以 stdio 提供 MCP 工具，连接已有 Nexus 服务")
    _client_options(mcp)

    call = sub.add_parser("call", help="向指定机器调用原生 Codex RPC")
    _client_options(call)
    call.add_argument("machine", help="机器 ID")
    call.add_argument("method", help="例如 thread/list 或 thread/fork")
    call.add_argument("--params", default="{}", help="原生参数 JSON")

    machines = sub.add_parser("machines", help="列出机器")
    _client_options(machines)

    threads = sub.add_parser("threads", help="列出会话")
    _client_options(threads)
    threads.add_argument("--machine", help="只查看指定机器 ID")
    threads.add_argument("--archived", action="store_true")
    threads.add_argument("--limit", type=int, default=50)

    imports = sub.add_parser("import-machines", help="从 JSON 配置添加或更新机器")
    _client_options(imports)
    imports.add_argument("file", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command not in {"serve", "service"}:
            args.url = args.url or default_url()
        if args.command == "service":
            from .system_service import manage_service

            parser.exit(manage_service(args))
        elif args.command == "serve":
            import uvicorn

            from .app import create_app

            config = uvicorn.Config(
                create_app(
                    data_dir=args.data_dir,
                    demo=args.demo,
                    demo_config=args.demo_config,
                    nexus_url=local_url(args.host, args.port),
                    tailscale_only=args.tailscale_only,
                    allowed_urls=args.allow_url,
                ),
                host=args.host or (tailscale_ip() if args.tailscale_only else "0.0.0.0"),
                port=args.port,
                log_level=args.log_level,
                timeout_graceful_shutdown=5,
            )
            server = uvicorn.Server(config)
            if args.tailscale_only:
                with (
                    config.bind_socket() as tailnet_socket,
                    socket.create_server(("127.0.0.1", args.port)) as loopback_socket,
                ):
                    server.run(sockets=[tailnet_socket, loopback_socket])
            else:
                server.run()
        elif args.command == "mcp":
            from .mcp_server import create_mcp_server

            create_mcp_server(args.url).run(transport="stdio")
        elif args.command == "call":
            params = json.loads(args.params)
            _print(
                _request(
                    args.url,
                    "POST",
                    f"/api/machines/{quote(args.machine, safe='')}/rpc",
                    json={"method": args.method, "params": params},
                )
            )
        elif args.command == "machines":
            _print(_request(args.url, "GET", "/api/machines"))
        elif args.command == "threads":
            _print(
                _request(
                    args.url,
                    "GET",
                    "/api/threads",
                    params={
                        **({"machineId": args.machine} if args.machine else {}),
                        "scope": "history" if args.archived else "loaded",
                        "archived": str(args.archived).lower(),
                        "limit": args.limit,
                    },
                )
            )
        elif args.command == "import-machines":
            config = json.loads(args.file.read_text())
            existing = _request(args.url, "GET", "/api/machines")["machines"]
            by_name = {machine["name"]: machine["id"] for machine in existing}
            result = []
            for machine in config["machines"]:
                machine_id = by_name.get(machine["name"])
                if machine_id:
                    saved = _request(
                        args.url,
                        "PATCH",
                        f"/api/machines/{quote(machine_id, safe='')}",
                        json=machine,
                    )
                else:
                    saved = _request(args.url, "POST", "/api/machines", json=machine)
                    by_name[machine["name"]] = saved["id"]
                result.append(saved)
            _print({"machines": result})
    except KeyboardInterrupt:
        return
    except (OSError, ValueError) as exc:
        parser.exit(1, f"nexus: {exc}\n")
    except subprocess.CalledProcessError as exc:
        parser.exit(exc.returncode, f"nexus: {exc.cmd[0]} 执行失败（退出码 {exc.returncode}）\n")
    except httpx.HTTPStatusError as exc:
        try:
            error = exc.response.json().get("error", {})
            message = error.get("message") or exc.response.text
        except ValueError:
            message = exc.response.text
        parser.exit(1, f"nexus: HTTP {exc.response.status_code}: {message}\n")
    except httpx.RequestError as exc:
        parser.exit(1, f"nexus: 无法连接服务：{exc}\n")


if __name__ == "__main__":
    main()
