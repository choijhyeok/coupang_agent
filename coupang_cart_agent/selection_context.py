from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .contracts import PriorPurchaseRecord, SelectionContext, SessionSelectionSignal, ShoppingRequest


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(slots=True)
class InMemorySelectionContextStore:
    """Development-friendly context store used by tests and demos."""

    prior_purchases_by_user: dict[str, list[PriorPurchaseRecord]] = field(default_factory=dict)
    session_signals_by_request: dict[str, list[SessionSelectionSignal]] = field(default_factory=dict)

    def load(self, request: ShoppingRequest) -> SelectionContext:
        return SelectionContext(
            prior_purchases=list(self.prior_purchases_by_user.get(request.user_id, [])),
            recent_session_signals=list(self.session_signals_by_request.get(request.request_id, [])),
        )


@dataclass(slots=True)
class SQLiteSelectionContextStore:
    """Read prior purchase and session signals from SQLite without mutating selection code."""

    database_path: str

    def load(self, request: ShoppingRequest) -> SelectionContext:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            return SelectionContext(
                prior_purchases=self._load_prior_purchases(connection, request.user_id),
                recent_session_signals=self._load_recent_session_signals(connection, request),
            )

    @staticmethod
    def _load_prior_purchases(
        connection: sqlite3.Connection,
        user_id: str,
    ) -> list[PriorPurchaseRecord]:
        rows = connection.execute(
            """
            SELECT product_id, product_name, purchase_count, last_purchased_at, satisfaction_rating
            FROM prior_purchases
            WHERE user_id = ?
            ORDER BY COALESCE(last_purchased_at, '') DESC, purchase_count DESC, product_id ASC
            """,
            (user_id,),
        ).fetchall()
        return [
            PriorPurchaseRecord(
                product_id=row["product_id"],
                product_name=row["product_name"],
                purchase_count=max(1, int(row["purchase_count"])),
                last_purchased_at=_parse_timestamp(row["last_purchased_at"]),
                satisfaction_rating=(
                    None if row["satisfaction_rating"] is None else float(row["satisfaction_rating"])
                ),
            )
            for row in rows
        ]

    @staticmethod
    def _load_recent_session_signals(
        connection: sqlite3.Connection,
        request: ShoppingRequest,
    ) -> list[SessionSelectionSignal]:
        rows = connection.execute(
            """
            SELECT product_id, signal, noted_at
            FROM recent_session_signals
            WHERE user_id = ?
              AND (request_id = ? OR request_id IS NULL)
            ORDER BY COALESCE(noted_at, '') DESC, product_id ASC
            """,
            (request.user_id, request.request_id),
        ).fetchall()
        return [
            SessionSelectionSignal(
                product_id=row["product_id"],
                signal=str(row["signal"]),
                noted_at=_parse_timestamp(row["noted_at"]),
            )
            for row in rows
        ]
