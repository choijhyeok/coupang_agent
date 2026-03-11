from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import RequestSession, ShoppingRequestEnvelope


class TelegramIntakeRepository:
    """Persist Telegram intake session and inbound request records."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def get_or_create_session(
        self,
        *,
        user_id: str,
        chat_id: str,
        occurred_at: datetime,
    ) -> RequestSession:
        session_id = self.build_session_id(user_id=user_id, chat_id=chat_id)
        occurred_at = occurred_at.astimezone(UTC)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, channel, user_id, chat_id, created_at, last_message_at
                FROM telegram_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO telegram_sessions (
                        session_id,
                        channel,
                        user_id,
                        chat_id,
                        created_at,
                        last_message_at
                    ) VALUES (?, 'telegram', ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        user_id,
                        chat_id,
                        occurred_at.isoformat(),
                        occurred_at.isoformat(),
                    ),
                )
                return RequestSession(
                    session_id=session_id,
                    channel="telegram",
                    user_id=user_id,
                    chat_id=chat_id,
                    created_at=occurred_at,
                    last_message_at=occurred_at,
                )

            created_at = datetime.fromisoformat(row[4])
            last_message_at = datetime.fromisoformat(row[5])
            updated_last_message_at = max(last_message_at, occurred_at)
            connection.execute(
                """
                UPDATE telegram_sessions
                SET last_message_at = ?
                WHERE session_id = ?
                """,
                (updated_last_message_at.isoformat(), session_id),
            )
            return RequestSession(
                session_id=row[0],
                channel=row[1],
                user_id=row[2],
                chat_id=row[3],
                created_at=created_at,
                last_message_at=updated_last_message_at,
            )

    def record_envelope(self, envelope: ShoppingRequestEnvelope) -> None:
        payload = json.dumps(envelope.raw_update, ensure_ascii=False, default=str)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO telegram_inbound_messages (
                    inbound_message_id,
                    session_id,
                    source,
                    mode,
                    update_id,
                    message_id,
                    user_id,
                    chat_id,
                    request_id,
                    raw_text,
                    parse_status,
                    error_message,
                    raw_update_json,
                    received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', NULL, ?, ?)
                """,
                (
                    envelope.inbound_message_id,
                    envelope.session.session_id,
                    envelope.source,
                    envelope.mode.value,
                    envelope.update_id,
                    envelope.message_id,
                    envelope.request.user_id,
                    envelope.request.chat_id,
                    envelope.request.request_id,
                    envelope.raw_text,
                    payload,
                    envelope.request.received_at.isoformat(),
                ),
            )

    def record_rejected_message(
        self,
        *,
        inbound_message_id: str,
        session: RequestSession | None,
        update_id: int | None,
        message_id: int | None,
        user_id: str | None,
        chat_id: str | None,
        raw_text: str,
        error_message: str,
        raw_update: dict[str, Any],
        occurred_at: datetime,
        mode: str,
    ) -> None:
        payload = json.dumps(raw_update, ensure_ascii=False, default=str)
        session_id = session.session_id if session is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO telegram_inbound_messages (
                    inbound_message_id,
                    session_id,
                    source,
                    mode,
                    update_id,
                    message_id,
                    user_id,
                    chat_id,
                    request_id,
                    raw_text,
                    parse_status,
                    error_message,
                    raw_update_json,
                    received_at
                ) VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?, NULL, ?, 'rejected', ?, ?, ?)
                """,
                (
                    inbound_message_id,
                    session_id,
                    mode,
                    update_id,
                    message_id,
                    user_id,
                    chat_id,
                    raw_text,
                    error_message,
                    payload,
                    occurred_at.astimezone(UTC).isoformat(),
                ),
            )

    def list_sessions(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id, channel, user_id, chat_id, created_at, last_message_at
                FROM telegram_sessions
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [
            {
                "session_id": row[0],
                "channel": row[1],
                "user_id": row[2],
                "chat_id": row[3],
                "created_at": row[4],
                "last_message_at": row[5],
            }
            for row in rows
        ]

    def list_inbound_messages(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    inbound_message_id,
                    session_id,
                    source,
                    mode,
                    update_id,
                    message_id,
                    user_id,
                    chat_id,
                    request_id,
                    raw_text,
                    parse_status,
                    error_message,
                    raw_update_json,
                    received_at
                FROM telegram_inbound_messages
                ORDER BY received_at ASC, inbound_message_id ASC
                """
            ).fetchall()
        return [
            {
                "inbound_message_id": row[0],
                "session_id": row[1],
                "source": row[2],
                "mode": row[3],
                "update_id": row[4],
                "message_id": row[5],
                "user_id": row[6],
                "chat_id": row[7],
                "request_id": row[8],
                "raw_text": row[9],
                "parse_status": row[10],
                "error_message": row[11],
                "raw_update": json.loads(row[12]),
                "received_at": row[13],
            }
            for row in rows
        ]

    @staticmethod
    def build_session_id(*, user_id: str, chat_id: str) -> str:
        return f"telegram-session:{chat_id}:{user_id}"

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_sessions (
                    session_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_inbound_messages (
                    inbound_message_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    source TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    update_id INTEGER,
                    message_id INTEGER,
                    user_id TEXT,
                    chat_id TEXT,
                    request_id TEXT,
                    raw_text TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    error_message TEXT,
                    raw_update_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES telegram_sessions(session_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)
