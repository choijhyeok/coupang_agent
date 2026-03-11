from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .contracts import CartAddResult


@dataclass(slots=True)
class CartResultRecord:
    recorded_at: datetime
    result: CartAddResult


class CartResultStore(Protocol):
    """Persistence seam for cart execution results and snapshots."""

    def save(self, record: CartResultRecord) -> None: ...


class SqliteCartResultStore:
    """Persist cart add results to a local SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def save(self, record: CartResultRecord) -> None:
        payload = json.dumps(asdict(record.result), ensure_ascii=False, default=str)
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO cart_add_results (
                    recorded_at,
                    success,
                    stage,
                    failure_reason,
                    product_id,
                    product_name,
                    quantity,
                    cart_item_id,
                    cart_count_before,
                    cart_count_after,
                    message,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.recorded_at.isoformat(),
                    int(record.result.success),
                    record.result.stage.value,
                    None if record.result.failure_reason is None else record.result.failure_reason.value,
                    record.result.selected_product.candidate.product_id,
                    record.result.selected_product.candidate.name,
                    record.result.selected_product.quantity,
                    record.result.cart_item_id,
                    record.result.cart_count_before,
                    record.result.cart_count_after,
                    record.result.message,
                    payload,
                ),
            )

    def fetch_all(self) -> list[dict[str, object]]:
        with sqlite3.connect(self._db_path) as connection:
            cursor = connection.execute(
                """
                SELECT
                    recorded_at,
                    success,
                    stage,
                    failure_reason,
                    product_id,
                    product_name,
                    quantity,
                    cart_item_id,
                    cart_count_before,
                    cart_count_after,
                    message,
                    payload_json
                FROM cart_add_results
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()

        records: list[dict[str, object]] = []
        for row in rows:
            records.append(
                {
                    "recorded_at": row[0],
                    "success": bool(row[1]),
                    "stage": row[2],
                    "failure_reason": row[3],
                    "product_id": row[4],
                    "product_name": row[5],
                    "quantity": row[6],
                    "cart_item_id": row[7],
                    "cart_count_before": row[8],
                    "cart_count_after": row[9],
                    "message": row[10],
                    "payload": json.loads(row[11]),
                }
            )
        return records

    def _initialize(self) -> None:
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cart_add_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    failure_reason TEXT,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    cart_item_id TEXT,
                    cart_count_before INTEGER,
                    cart_count_after INTEGER,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )


def build_cart_result_record(result: CartAddResult) -> CartResultRecord:
    return CartResultRecord(
        recorded_at=datetime.now(UTC),
        result=result,
    )
