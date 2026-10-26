import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


class Storage:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    received_at TEXT
                );
                CREATE TABLE IF NOT EXISTS device (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_seen TEXT
                );
                INSERT OR IGNORE INTO device (id) VALUES (1);
            """)

    def create_message(self, text: str, now: datetime) -> dict:
        with closing(self.connect()) as connection, connection:
            row = connection.execute(
                "INSERT INTO messages (text, created_at) VALUES (?, ?) RETURNING *",
                (text, now.isoformat()),
            ).fetchone()
            return dict(row)

    def latest_message(self) -> dict | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id, text FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def ping(self, now: datetime) -> bool:
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "UPDATE device SET last_seen = ? WHERE id = 1", (now.isoformat(),)
            )
            row = connection.execute(
                "SELECT received_at FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row is not None and row["received_at"] is None

    def acknowledge(self, message_id: int, now: datetime) -> bool:
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE messages SET received_at = COALESCE(received_at, ?) WHERE id = ?",
                (now.isoformat(), message_id),
            )
            return cursor.rowcount == 1

    def state(self, now: datetime, ping_interval: int) -> dict:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            last_seen = connection.execute(
                "SELECT last_seen FROM device WHERE id = 1"
            ).fetchone()["last_seen"]
            messages = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM messages ORDER BY id DESC LIMIT 50"
                )
            ]
        for index, message in enumerate(messages):
            message["is_current"] = index == 0
        status = "never_seen"
        if last_seen is not None:
            age = now - datetime.fromisoformat(last_seen)
            status = "online" if age <= timedelta(seconds=3 * ping_interval) else "offline"
        return {
            "device": {
                "status": status,
                "last_seen": last_seen,
                "ping_interval_seconds": ping_interval,
            },
            "messages": messages,
        }
