from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .azure_openai import AgentPlan
from .contracts import (
    CartAddResult,
    NotificationPayload,
    PriorPurchaseRecord,
    RequestSession,
    SelectionContext,
    SessionSelectionSignal,
    SelectedProduct,
    ShoppingRequestEnvelope,
)


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class PostgresOperationalStore:
    """Operational persistence for live workflow metadata and operator-visible state."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN must not be empty.")
        self._dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_threads (
                        thread_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        last_request_id TEXT,
                        last_status TEXT NOT NULL DEFAULT 'received',
                        last_failure_stage TEXT,
                        updated_at TIMESTAMPTZ NOT NULL,
                        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_runs (
                        id BIGSERIAL PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        success BOOLEAN NOT NULL,
                        failed_stage TEXT,
                        failure_message TEXT,
                        request_envelope_json JSONB NOT NULL,
                        agent_plan_json JSONB,
                        agent_reasoning_summary TEXT,
                        last_observation_json JSONB,
                        agent_steps_json JSONB,
                        performance_json JSONB,
                        selections_json JSONB NOT NULL,
                        cart_results_json JSONB NOT NULL,
                        notification_payload_json JSONB,
                        recorded_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS prior_purchases (
                        user_id TEXT NOT NULL,
                        product_id TEXT NOT NULL,
                        product_name TEXT NOT NULL,
                        purchase_count INTEGER NOT NULL,
                        last_purchased_at TIMESTAMPTZ,
                        satisfaction_rating DOUBLE PRECISION,
                        PRIMARY KEY (user_id, product_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recent_session_signals (
                        id BIGSERIAL PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        request_id TEXT,
                        product_id TEXT NOT NULL,
                        signal TEXT NOT NULL,
                        noted_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS current_cart_snapshot_items (
                        id BIGSERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        product_id TEXT NOT NULL,
                        product_name TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        unit_price_krw INTEGER NOT NULL,
                        total_price_krw INTEGER NOT NULL,
                        snapshot_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                self._ensure_column(
                    cursor,
                    table_name="workflow_runs",
                    column_name="agent_reasoning_summary",
                    ddl="ALTER TABLE workflow_runs ADD COLUMN agent_reasoning_summary TEXT",
                )
                self._ensure_column(
                    cursor,
                    table_name="workflow_runs",
                    column_name="last_observation_json",
                    ddl="ALTER TABLE workflow_runs ADD COLUMN last_observation_json JSONB",
                )
                self._ensure_column(
                    cursor,
                    table_name="workflow_runs",
                    column_name="agent_steps_json",
                    ddl="ALTER TABLE workflow_runs ADD COLUMN agent_steps_json JSONB",
                )
                self._ensure_column(
                    cursor,
                    table_name="workflow_runs",
                    column_name="performance_json",
                    ddl="ALTER TABLE workflow_runs ADD COLUMN performance_json JSONB",
                )
            connection.commit()

    def ping(self) -> dict[str, object]:
        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database() AS database_name, NOW() AS checked_at")
                database_name, checked_at = cursor.fetchone()
        return {
            "ok": True,
            "database_name": database_name,
            "checked_at": checked_at.isoformat(),
        }

    def record_intake(self, *, thread_id: str, envelope: ShoppingRequestEnvelope) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO workflow_threads (
                        thread_id,
                        user_id,
                        chat_id,
                        session_id,
                        last_request_id,
                        last_status,
                        last_failure_stage,
                        updated_at,
                        metadata_json
                    ) VALUES (%s, %s, %s, %s, %s, 'received', NULL, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        chat_id = EXCLUDED.chat_id,
                        session_id = EXCLUDED.session_id,
                        last_request_id = EXCLUDED.last_request_id,
                        last_status = EXCLUDED.last_status,
                        last_failure_stage = NULL,
                        updated_at = EXCLUDED.updated_at,
                        metadata_json = EXCLUDED.metadata_json
                    """,
                    (
                        thread_id,
                        envelope.request.user_id,
                        envelope.request.chat_id,
                        envelope.session.session_id,
                        envelope.request.request_id,
                        envelope.request.received_at,
                        Jsonb(
                            _json_ready(
                                {
                                "mode": envelope.mode.value,
                                "source": envelope.source,
                                "update_id": envelope.update_id,
                                "message_id": envelope.message_id,
                                }
                            )
                        ),
                    ),
                )
            connection.commit()

    def load_selection_context(self, *, user_id: str, thread_id: str) -> SelectionContext:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            prior_rows = connection.execute(
                """
                SELECT product_id, product_name, purchase_count, last_purchased_at, satisfaction_rating
                FROM prior_purchases
                WHERE user_id = %s
                ORDER BY COALESCE(last_purchased_at, TIMESTAMPTZ 'epoch') DESC, purchase_count DESC, product_id ASC
                LIMIT 10
                """,
                (user_id,),
            ).fetchall()
            signal_rows = connection.execute(
                """
                SELECT product_id, signal, noted_at
                FROM recent_session_signals
                WHERE user_id = %s
                  AND thread_id = %s
                ORDER BY noted_at DESC, product_id ASC
                LIMIT 10
                """,
                (user_id, thread_id),
            ).fetchall()

        return SelectionContext(
            prior_purchases=[
                PriorPurchaseRecord(
                    product_id=str(row["product_id"]),
                    product_name=str(row["product_name"]),
                    purchase_count=max(1, int(row["purchase_count"])),
                    last_purchased_at=_parse_timestamp(row["last_purchased_at"]),
                    satisfaction_rating=(
                        None if row["satisfaction_rating"] is None else float(row["satisfaction_rating"])
                    ),
                )
                for row in prior_rows
            ],
            recent_session_signals=[
                SessionSelectionSignal(
                    product_id=str(row["product_id"]),
                    signal=str(row["signal"]),
                    noted_at=_parse_timestamp(row["noted_at"]),
                )
                for row in signal_rows
            ],
        )

    def load_notification_context(self, *, user_id: str) -> dict[str, object]:
        context = self.load_selection_context(user_id=user_id, thread_id="")
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT product_id, product_name, quantity, unit_price_krw, total_price_krw, snapshot_at
                FROM current_cart_snapshot_items
                WHERE user_id = %s
                  AND snapshot_at = (
                    SELECT MAX(snapshot_at)
                    FROM current_cart_snapshot_items
                    WHERE user_id = %s
                  )
                ORDER BY product_name ASC, product_id ASC
                """,
                (user_id, user_id),
            ).fetchall()

        return {
            "cart_snapshot_items": [
                {
                    "product_id": str(row["product_id"]),
                    "name": str(row["product_name"]),
                    "quantity": max(1, int(row["quantity"])),
                    "price_krw": int(row["unit_price_krw"]),
                    "line_total_krw": int(row["total_price_krw"]),
                    "snapshot_at": _parse_timestamp(row["snapshot_at"]).isoformat(),
                }
                for row in rows
            ],
            "prior_purchases": context.prior_purchases,
        }

    def load_thread_context(self, *, thread_id: str) -> dict[str, object]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT thread_id, user_id, chat_id, session_id, last_request_id, last_status, last_failure_stage, updated_at
                FROM workflow_threads
                WHERE thread_id = %s
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return {}
        return {
            "thread_id": str(row["thread_id"]),
            "user_id": str(row["user_id"]),
            "chat_id": str(row["chat_id"]),
            "session_id": str(row["session_id"]),
            "last_request_id": row["last_request_id"],
            "last_status": str(row["last_status"]),
            "last_failure_stage": row["last_failure_stage"],
            "updated_at": _parse_timestamp(row["updated_at"]).isoformat(),
        }

    def record_run(
        self,
        *,
        thread_id: str,
        envelope: ShoppingRequestEnvelope,
        agent_plan: AgentPlan | None,
        selections: list[SelectedProduct],
        cart_results: list[CartAddResult],
        notification_payload: NotificationPayload | None,
        agent_reasoning_summary: str | None,
        last_observation: dict[str, object] | None,
        agent_steps: list[dict[str, object]] | None,
        performance: dict[str, object] | None,
        success: bool,
        failed_stage: str | None,
        failure_message: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE workflow_threads
                    SET user_id = %s,
                        chat_id = %s,
                        session_id = %s,
                        last_request_id = %s,
                        last_status = %s,
                        last_failure_stage = %s,
                        updated_at = %s,
                        metadata_json = metadata_json || %s
                    WHERE thread_id = %s
                    """,
                    (
                        envelope.request.user_id,
                        envelope.request.chat_id,
                        envelope.session.session_id,
                        envelope.request.request_id,
                        "success" if success else "failed",
                        failed_stage,
                        now,
                        Jsonb(
                            _json_ready(
                                {
                                "last_notification_stage": (
                                    None if notification_payload is None else notification_payload.stage
                                ),
                                "last_mode": envelope.mode.value,
                                "agent_reasoning_summary": agent_reasoning_summary,
                                "last_performance": performance,
                                }
                            )
                        ),
                        thread_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO workflow_runs (
                        thread_id,
                        user_id,
                        chat_id,
                        request_id,
                        success,
                        failed_stage,
                        failure_message,
                        request_envelope_json,
                        agent_plan_json,
                        agent_reasoning_summary,
                        last_observation_json,
                        agent_steps_json,
                        performance_json,
                        selections_json,
                        cart_results_json,
                        notification_payload_json,
                        recorded_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        thread_id,
                        envelope.request.user_id,
                        envelope.request.chat_id,
                        envelope.request.request_id,
                        success,
                        failed_stage,
                        failure_message,
                        Jsonb(_json_ready(asdict(envelope))),
                        None if agent_plan is None else Jsonb(_json_ready(agent_plan.as_dict())),
                        agent_reasoning_summary,
                        None if last_observation is None else Jsonb(_json_ready(last_observation)),
                        None if agent_steps is None else Jsonb(_json_ready(agent_steps)),
                        None if performance is None else Jsonb(_json_ready(performance)),
                        Jsonb(_json_ready([asdict(selection) for selection in selections])),
                        Jsonb(_json_ready([asdict(result) for result in cart_results])),
                        None if notification_payload is None else Jsonb(_json_ready(asdict(notification_payload))),
                        now,
                    ),
                )

                if success and cart_results:
                    cursor.execute(
                        "DELETE FROM current_cart_snapshot_items WHERE user_id = %s",
                        (envelope.request.user_id,),
                    )
                    for result in cart_results:
                        candidate = result.selected_product.candidate
                        quantity = result.selected_product.quantity
                        cursor.execute(
                            """
                            INSERT INTO current_cart_snapshot_items (
                                user_id,
                                thread_id,
                                product_id,
                                product_name,
                                quantity,
                                unit_price_krw,
                                total_price_krw,
                                snapshot_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                envelope.request.user_id,
                                thread_id,
                                candidate.product_id,
                                candidate.name,
                                quantity,
                                candidate.price_krw,
                                candidate.price_krw * quantity,
                                now,
                            ),
                        )
                        cursor.execute(
                            """
                            INSERT INTO prior_purchases (
                                user_id,
                                product_id,
                                product_name,
                                purchase_count,
                                last_purchased_at,
                                satisfaction_rating
                            ) VALUES (%s, %s, %s, 1, %s, %s)
                            ON CONFLICT (user_id, product_id) DO UPDATE SET
                                product_name = EXCLUDED.product_name,
                                purchase_count = prior_purchases.purchase_count + 1,
                                last_purchased_at = EXCLUDED.last_purchased_at
                            """,
                            (
                                envelope.request.user_id,
                                candidate.product_id,
                                candidate.name,
                                now,
                                candidate.rating,
                            ),
                        )

                for selection in selections:
                    cursor.execute(
                        """
                        INSERT INTO recent_session_signals (
                            thread_id,
                            user_id,
                            request_id,
                            product_id,
                            signal,
                            noted_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            thread_id,
                            envelope.request.user_id,
                            envelope.request.request_id,
                            selection.candidate.product_id,
                            "preferred",
                            now,
                        ),
                    )
            connection.commit()

    @staticmethod
    def session_to_dict(session: RequestSession) -> dict[str, object]:
        return asdict(session)

    def fetch_workflow_runs(self) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT thread_id, user_id, chat_id, request_id, success, failed_stage, failure_message, performance_json, recorded_at
                FROM workflow_runs
                ORDER BY id ASC
                """
            ).fetchall()
        return [
            {
                "thread_id": str(row["thread_id"]),
                "user_id": str(row["user_id"]),
                "chat_id": str(row["chat_id"]),
                "request_id": str(row["request_id"]),
                "success": bool(row["success"]),
                "failed_stage": row["failed_stage"],
                "failure_message": row["failure_message"],
                "performance": row["performance_json"] or {},
                "recorded_at": _parse_timestamp(row["recorded_at"]).isoformat(),
            }
            for row in rows
        ]

    @staticmethod
    def _ensure_column(cursor, *, table_name: str, column_name: str, ddl: str) -> None:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = %s
              AND column_name = %s
            """,
            (table_name, column_name),
        )
        if cursor.fetchone() is None:
            cursor.execute(ddl)


def _json_ready(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
