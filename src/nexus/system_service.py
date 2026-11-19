"""Opt-in Linux user service installation and lifecycle commands."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

UNIT = "nexus.service"
ENVIRONMENT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "NEXUS_PUSH_SUBJECT",
)


def _unit_quote(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False).replace("%", "%%")


def _environment_quote(value: str) -> str:
    for character in ("\\", '"', "$", "`"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def _unit_text(args: argparse.Namespace, environment_file: Path) -> str:
    data_dir = (
        Path(
            args.data_dir
            or os.environ.get(
                "NEXUS_DATA_DIR",
                Path.home() / ".local/share/nexus",
            )
        )
        .expanduser()
        .resolve()
    )
    # Keep the virtualenv executable path: resolving its symlink loses the environment.
    command = [
        sys.executable,
        "-m",
        "nexus",
        "serve",
        "--data-dir",
        str(data_dir),
        "--port",
        str(args.port),
        "--log-level",
        args.log_level,
    ]
    if args.tailscale_only:
        command.append("--tailscale-only")
    elif args.host:
        command.extend(("--host", args.host))
    for url in args.allow_url:
        command.extend(("--allow-url", url))
    exec_start = " ".join(_unit_quote(part) for part in command)
    # Nexus closes its own connections; the shared Codex daemon survives Nexus restarts.
    return f"""[Unit]
Description=Nexus Codex console

[Service]
Type=exec
WorkingDirectory={str(Path.cwd()).replace("%", "%%")}
ExecStart=:{exec_start}
Environment={_unit_quote("PATH=" + os.environ.get("PATH", os.defpath))}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile={str(environment_file).replace("%", "%%")}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
KillMode=process

[Install]
WantedBy=default.target
"""


def _systemctl(*arguments: str) -> None:
    subprocess.run(["systemctl", "--user", *arguments], check=True)


def manage_service(args: argparse.Namespace) -> int:
    if sys.platform != "linux" or not shutil.which("systemctl"):
        raise ValueError("自启管理需要 Linux 和用户级 systemd。")
    config_dir = (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser().resolve()
    )
    unit_file = config_dir / "systemd/user" / UNIT
    environment_file = config_dir / "nexus/service.env"

    if args.action == "install":
        unit_file.parent.mkdir(parents=True, exist_ok=True)
        environment_file.parent.mkdir(parents=True, exist_ok=True)
        if not environment_file.exists():
            descriptor = os.open(environment_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for key in ENVIRONMENT_KEYS:
                    if key in os.environ:
                        output.write(f"{key}={_environment_quote(os.environ[key])}\n")
        unit_file.write_text(_unit_text(args, environment_file), encoding="utf-8")
        _systemctl("daemon-reload")
        print(f"已安装服务配置：{unit_file}")
        print("开启自启：nexus service enable；应用到已运行的服务：nexus service restart")
    elif args.action == "status":
        subprocess.run(["loginctl", "show-user", str(os.getuid()), "--property=Linger"], check=True)
        return subprocess.run(
            ["systemctl", "--user", "status", "--no-pager", "--full", UNIT]
        ).returncode
    elif args.action == "logs":
        command = ["journalctl", "--user", "--unit", UNIT, "--no-pager", "--lines", str(args.lines)]
        if args.follow:
            command.append("--follow")
        return subprocess.run(command).returncode
    else:
        if not unit_file.exists():
            raise ValueError("请先运行 nexus service install 安装服务配置。")
        if args.action == "enable":
            try:
                subprocess.run(
                    ["loginctl", "--no-ask-password", "enable-linger", str(os.getuid())], check=True
                )
            except subprocess.CalledProcessError as exc:
                user = shlex.quote(getpass.getuser())
                raise ValueError(
                    f"请先执行 sudo loginctl enable-linger {user}，再运行 nexus service enable。"
                ) from exc
            _systemctl("enable", "--now", UNIT)
            print("已开启开机自启并启动 Nexus，无需登录。")
        elif args.action == "disable":
            _systemctl("disable", "--now", UNIT)
            print("已关闭开机自启并停止 Nexus。")
        elif args.action == "restart":
            _systemctl("restart", UNIT)
        elif args.action == "uninstall":
            _systemctl("disable", "--now", UNIT)
            unit_file.unlink()
            _systemctl("daemon-reload")
            print("已卸载服务，数据和环境配置保留。")
    return 0
