"""Errors shared by the control service and runtime adapter."""

from typing import Any


class NexusError(Exception):
    def __init__(
        self, message: str, code: int | str | None = None, data: Any = None, status_code: int = 400
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data
        self.status_code = status_code
