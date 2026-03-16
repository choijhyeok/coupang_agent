from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import IntakeMode, RequestSession, RequestedItem, ShoppingRequest, ShoppingRequestEnvelope


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
        request_payload = json.dumps(asdict(envelope.request), ensure_ascii=False, default=str)
        envelope_payload = json.dumps(asdict(envelope), ensure_ascii=False, default=str)
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
                    request_json,
                    envelope_json,
                    raw_text,
                    parse_status,
                    workflow_status,
                    workflow_error,
                    error_message,
                    raw_update_json,
                    received_at,
                    processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', 'pending', NULL, NULL, ?, ?, NULL)
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
                    request_payload,
                    envelope_payload,
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
                    request_json,
                    envelope_json,
                    raw_text,
                    parse_status,
                    workflow_status,
                    workflow_error,
                    error_message,
                    raw_update_json,
                    received_at,
                    processed_at
                ) VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 'rejected', 'rejected', NULL, ?, ?, ?, NULL)
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

    def load_pending_envelopes(self, *, limit: int = 20) -> list[ShoppingRequestEnvelope]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT envelope_json
                FROM telegram_inbound_messages
                WHERE parse_status = 'parsed' AND workflow_status IN ('pending', 'processing')
                ORDER BY received_at ASC, inbound_message_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        envelopes: list[ShoppingRequestEnvelope] = []
        for row in rows:
            if not row[0]:
                continue
            envelopes.append(self._deserialize_envelope(json.loads(row[0])))
        return envelopes

    def mark_envelope_processing(self, *, inbound_message_id: str) -> None:
        self._update_workflow_status(
            inbound_message_id=inbound_message_id,
            status="processing",
            workflow_error=None,
            processed_at=None,
        )

    def mark_envelope_completed(
        self,
        *,
        inbound_message_id: str,
        workflow_error: str | None = None,
    ) -> None:
        self._update_workflow_status(
            inbound_message_id=inbound_message_id,
            status="completed",
            workflow_error=workflow_error,
            processed_at=datetime.now(UTC).isoformat(),
        )

    def mark_envelope_failed(self, *, inbound_message_id: str, workflow_error: str) -> None:
        self._update_workflow_status(
            inbound_message_id=inbound_message_id,
            status="failed",
            workflow_error=workflow_error,
            processed_at=datetime.now(UTC).isoformat(),
        )

    def load_worker_cursor(self, *, worker_name: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT next_offset
                FROM telegram_worker_state
                WHERE worker_name = ?
                """,
                (worker_name,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def save_worker_cursor(
        self,
        *,
        worker_name: str,
        next_offset: int | None,
        last_update_id: int | None,
        last_result_json: dict[str, object] | None = None,
    ) -> None:
        payload = None if last_result_json is None else json.dumps(last_result_json, ensure_ascii=False, default=str)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_worker_state (
                    worker_name,
                    next_offset,
                    last_update_id,
                    last_poll_at,
                    updated_at,
                    last_result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_name) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    last_update_id = excluded.last_update_id,
                    last_poll_at = excluded.last_poll_at,
                    updated_at = excluded.updated_at,
                    last_result_json = excluded.last_result_json
                """,
                (
                    worker_name,
                    next_offset,
                    last_update_id,
                    now,
                    now,
                    payload,
                ),
            )

    def list_worker_state(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT worker_name, next_offset, last_update_id, last_poll_at, updated_at, last_result_json
                FROM telegram_worker_state
                ORDER BY worker_name ASC
                """
            ).fetchall()
        return [
            {
                "worker_name": row[0],
                "next_offset": row[1],
                "last_update_id": row[2],
                "last_poll_at": row[3],
                "updated_at": row[4],
                "last_result": None if row[5] is None else json.loads(row[5]),
            }
            for row in rows
        ]

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
                    request_json,
                    envelope_json,
                    raw_text,
                    parse_status,
                    workflow_status,
                    workflow_error,
                    error_message,
                    raw_update_json,
                    received_at,
                    processed_at
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
                "request": None if row[9] is None else json.loads(row[9]),
                "envelope": None if row[10] is None else json.loads(row[10]),
                "raw_text": row[11],
                "parse_status": row[12],
                "workflow_status": row[13],
                "workflow_error": row[14],
                "error_message": row[15],
                "raw_update": json.loads(row[16]),
                "received_at": row[17],
                "processed_at": row[18],
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
                    request_json TEXT,
                    envelope_json TEXT,
                    raw_text TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    workflow_status TEXT NOT NULL DEFAULT 'pending',
                    workflow_error TEXT,
                    error_message TEXT,
                    raw_update_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES telegram_sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_worker_state (
                    worker_name TEXT PRIMARY KEY,
                    next_offset INTEGER,
                    last_update_id INTEGER,
                    last_poll_at TEXT,
                    updated_at TEXT NOT NULL,
                    last_result_json TEXT
                )
                """
            )
            self._apply_migrations(connection)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(telegram_inbound_messages)").fetchall()
        }
        migrations = {
            "request_json": "ALTER TABLE telegram_inbound_messages ADD COLUMN request_json TEXT",
            "envelope_json": "ALTER TABLE telegram_inbound_messages ADD COLUMN envelope_json TEXT",
            "workflow_status": "ALTER TABLE telegram_inbound_messages ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'pending'",
            "workflow_error": "ALTER TABLE telegram_inbound_messages ADD COLUMN workflow_error TEXT",
            "processed_at": "ALTER TABLE telegram_inbound_messages ADD COLUMN processed_at TEXT",
        }
        for column_name, statement in migrations.items():
            if column_name not in columns:
                connection.execute(statement)

    def _update_workflow_status(
        self,
        *,
        inbound_message_id: str,
        status: str,
        workflow_error: str | None,
        processed_at: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telegram_inbound_messages
                SET workflow_status = ?, workflow_error = ?, processed_at = ?
                WHERE inbound_message_id = ?
                """,
                (status, workflow_error, processed_at, inbound_message_id),
            )

    def _deserialize_envelope(self, raw: dict[str, Any]) -> ShoppingRequestEnvelope:
        request_raw = raw["request"]
        session_raw = raw["session"]
        return ShoppingRequestEnvelope(
            source=str(raw["source"]),
            mode=IntakeMode(str(raw["mode"])),
            request=ShoppingRequest(
                user_id=str(request_raw["user_id"]),
                chat_id=str(request_raw["chat_id"]),
                items=[
                    RequestedItem(
                        name=str(item["name"]),
                        quantity=int(item.get("quantity", 1)),
                        constraints=[str(constraint) for constraint in item.get("constraints", [])],
                        max_price_krw=None
                        if item.get("max_price_krw") is None
                        else int(item["max_price_krw"]),
                        explicit_brand=None if item.get("explicit_brand") is None else str(item["explicit_brand"]),
                        explicit_unit_size=(
                            None if item.get("explicit_unit_size") is None else str(item["explicit_unit_size"])
                        ),
                        explicit_pack_count=(
                            None if item.get("explicit_pack_count") is None else int(item["explicit_pack_count"])
                        ),
                        explicit_pack_unit=(
                            None if item.get("explicit_pack_unit") is None else str(item["explicit_pack_unit"])
                        ),
                    )
                    for item in request_raw.get("items", [])
                ],
                raw_text=str(request_raw["raw_text"]),
                request_id=str(request_raw["request_id"]),
                received_at=datetime.fromisoformat(str(request_raw["received_at"])),
            ),
            session=RequestSession(
                session_id=str(session_raw["session_id"]),
                channel=str(session_raw["channel"]),
                user_id=str(session_raw["user_id"]),
                chat_id=str(session_raw["chat_id"]),
                created_at=datetime.fromisoformat(str(session_raw["created_at"])),
                last_message_at=datetime.fromisoformat(str(session_raw["last_message_at"])),
            ),
            inbound_message_id=str(raw["inbound_message_id"]),
            update_id=None if raw.get("update_id") is None else int(raw["update_id"]),
            message_id=None if raw.get("message_id") is None else int(raw["message_id"]),
            raw_text=str(raw.get("raw_text", "")),
            raw_update=dict(raw.get("raw_update", {})),
            metadata=dict(raw.get("metadata", {})),
        )
