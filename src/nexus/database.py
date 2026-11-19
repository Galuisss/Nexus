"""Shared SQLite connection for the search index and message records."""

import sqlite3
import threading
from pathlib import Path


class Database:
    def __init__(self, directory: Path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(directory / "search.sqlite", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")

    def close(self) -> None:
        with self.lock:
            self.connection.close()
