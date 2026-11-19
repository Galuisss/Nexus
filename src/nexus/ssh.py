"""Shared system SSH connection settings."""

SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)


def ssh_target(machine: dict) -> str:
    host = machine["host"]
    return f"{machine['user']}@{host}" if machine.get("user") else host
