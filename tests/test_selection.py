from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from coupang_cart_agent.candidate_sources import CapturedCoupangFixtureCandidateSource, product_candidate_from_record
from coupang_cart_agent.contracts import ProductCandidate, RequestedItem, ShoppingRequest
from coupang_cart_agent.selection import HeuristicProductSelectionService, select_best_product
from coupang_cart_agent.selection_context import InMemorySelectionContextStore, SQLiteSelectionContextStore


def candidate(
    *,
    product_id: str,
    name: str = "코카콜라 제로 355ml",
    price_krw: int,
    rating: float,
    review_count: int,
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name=name,
        price_krw=price_krw,
        rating=rating,
        review_count=review_count,
        product_url=f"https://www.coupang.com/vp/products/{product_id}",
    )


class SelectionTests(unittest.TestCase):
    def test_select_best_product_prefers_balanced_quality_and_price(self) -> None:
        requested_item = RequestedItem(name="코카콜라 제로 355ml", quantity=2)
        selected = select_best_product(
            requested_item,
            [
                candidate(product_id="cheap-low", price_krw=9900, rating=3.8, review_count=18),
                candidate(product_id="balanced", price_krw=14800, rating=4.8, review_count=3200),
                candidate(product_id="premium", price_krw=19200, rating=4.9, review_count=2400),
            ],
        )

        self.assertEqual(selected.candidate.product_id, "balanced")
        self.assertEqual(selected.quantity, 2)
        self.assertIn("평점 4.8/5", selected.selection_reason)
        self.assertIn("리뷰 3,200개", selected.selection_reason)

    def test_select_best_product_does_not_always_choose_the_cheapest_item(self) -> None:
        requested_item = RequestedItem(name="생수 2L")
        selected = select_best_product(
            requested_item,
            [
                candidate(product_id="too-cheap", price_krw=4900, rating=3.5, review_count=7),
                candidate(product_id="safe-choice", price_krw=7600, rating=4.7, review_count=1500),
                candidate(product_id="expensive", price_krw=9900, rating=4.8, review_count=900),
            ],
        )

        self.assertEqual(selected.candidate.product_id, "safe-choice")
        self.assertGreater(selected.score, 0)

    def test_select_best_product_breaks_ties_by_rating_then_reviews(self) -> None:
        requested_item = RequestedItem(name="컵라면")
        selected = select_best_product(
            requested_item,
            [
                candidate(product_id="first", price_krw=10000, rating=4.6, review_count=1000),
                candidate(product_id="second", price_krw=10000, rating=4.6, review_count=1100),
                candidate(product_id="third", price_krw=10000, rating=4.7, review_count=900),
            ],
        )

        self.assertEqual(selected.candidate.product_id, "third")

    def test_select_best_product_penalizes_review_poor_candidates(self) -> None:
        requested_item = RequestedItem(name="물티슈")
        selected = select_best_product(
            requested_item,
            [
                candidate(product_id="few-reviews", price_krw=8900, rating=4.9, review_count=4),
                candidate(product_id="trusted", price_krw=9400, rating=4.7, review_count=820),
                candidate(product_id="premium", price_krw=11000, rating=4.8, review_count=1200),
            ],
        )

        self.assertEqual(selected.candidate.product_id, "trusted")

    def test_select_best_product_respects_explicit_brand_and_pack_constraints(self) -> None:
        requested_item = RequestedItem(
            name="삼다수 2L",
            explicit_brand="삼다수",
            explicit_unit_size="2l",
            explicit_pack_count=1,
            explicit_pack_unit="개",
        )
        selected = select_best_product(
            requested_item,
            [
                candidate(
                    product_id="wrong-brand-6pack",
                    name="몽베스트 생수, 2L, 6개",
                    price_krw=6800,
                    rating=4.9,
                    review_count=21000,
                ),
                candidate(
                    product_id="right-brand-1pack",
                    name="삼다수 생수, 2L, 1개",
                    price_krw=1300,
                    rating=4.7,
                    review_count=8300,
                ),
                candidate(
                    product_id="right-brand-6pack",
                    name="삼다수 생수, 2L, 6개",
                    price_krw=7200,
                    rating=4.8,
                    review_count=12000,
                ),
            ],
        )

        self.assertEqual(selected.candidate.product_id, "right-brand-1pack")
        self.assertIn("요청 조건 반영", selected.selection_reason)

    def test_select_best_product_fails_safely_when_only_pack_mismatches_exist(self) -> None:
        requested_item = RequestedItem(
            name="삼다수 2L",
            explicit_brand="삼다수",
            explicit_unit_size="2l",
            explicit_pack_count=1,
            explicit_pack_unit="개",
        )

        with self.assertRaises(ValueError) as context:
            select_best_product(
                requested_item,
                [
                    candidate(
                        product_id="six-pack-a",
                        name="삼다수 생수, 2L, 6개",
                        price_krw=6900,
                        rating=4.8,
                        review_count=12000,
                    ),
                    candidate(
                        product_id="six-pack-b",
                        name="삼다수 생수, 2L, 12개",
                        price_krw=12800,
                        rating=4.9,
                        review_count=18000,
                    ),
                    candidate(
                        product_id="wrong-brand",
                        name="몽베스트 생수, 2L, 6개",
                        price_krw=6400,
                        rating=4.9,
                        review_count=22000,
                    ),
                ],
            )

        self.assertIn("explicit request constraints", str(context.exception))
        self.assertIn("pack mismatch", str(context.exception))

    def test_select_best_product_rejects_small_candidate_sets(self) -> None:
        requested_item = RequestedItem(name="라면")

        with self.assertRaises(ValueError):
            select_best_product(
                requested_item,
                [
                    candidate(product_id="one", price_krw=5000, rating=4.5, review_count=100),
                    candidate(product_id="two", price_krw=5500, rating=4.6, review_count=120),
                ],
            )

    def test_service_select_products_returns_selected_product_contracts(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:1",
            chat_id="1",
            raw_text="제로콜라 1개 담아줘",
            items=[RequestedItem(name="제로콜라", quantity=1)],
        )
        service = HeuristicProductSelectionService()

        selections = service.select_products(
            request,
            {
                "제로콜라": [
                    candidate(product_id="a", name="제로콜라", price_krw=11000, rating=4.3, review_count=300),
                    candidate(product_id="b", name="제로콜라", price_krw=9800, rating=4.7, review_count=1200),
                    candidate(product_id="c", name="제로콜라", price_krw=13000, rating=4.8, review_count=900),
                ]
            },
        )

        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].request_item_name, "제로콜라")
        self.assertEqual(selections[0].candidate.product_id, "b")

    def test_service_uses_prior_purchase_and_session_context(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:1",
            chat_id="1",
            raw_text="세제 담아줘",
            request_id="request-ctx-1",
            items=[RequestedItem(name="세제", quantity=1)],
        )
        service = HeuristicProductSelectionService(
            context_store=InMemorySelectionContextStore(
                prior_purchases_by_user={
                    "telegram:1": [
                        product_candidate_to_purchase_record(
                            product_id="trusted-repeat",
                            product_name="세제 베스트",
                            purchase_count=2,
                            satisfaction_rating=4.8,
                        )
                    ]
                },
                session_signals_by_request={
                    "request-ctx-1": [session_signal(product_id="avoid-me", signal="avoid")]
                },
            )
        )

        selections = service.select_products(
            request,
            {
                "세제": [
                    candidate(product_id="avoid-me", name="세제", price_krw=8300, rating=4.8, review_count=2300),
                    candidate(
                        product_id="trusted-repeat",
                        name="세제",
                        price_krw=8800,
                        rating=4.7,
                        review_count=1800,
                    ),
                    candidate(product_id="new-premium", name="세제", price_krw=11900, rating=4.9, review_count=420),
                ]
            },
        )

        self.assertEqual(selections[0].candidate.product_id, "trusted-repeat")
        self.assertIn("이전 구매 이력 2회", selections[0].selection_reason)

    def test_captured_fixture_source_supports_production_shaped_candidates(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:fixture-user",
            chat_id="fixture-chat",
            raw_text="양파 담아줘",
            request_id="fixture-request-1",
            items=[RequestedItem(name="양파", quantity=1)],
        )
        fixture_path = Path(__file__).parent / "fixtures" / "coupang_search_onion_fixture.json"
        source = CapturedCoupangFixtureCandidateSource(fixture_path=str(fixture_path))

        candidates_by_item = source(request)
        selections = HeuristicProductSelectionService().select_products(request, candidates_by_item)

        self.assertEqual(len(candidates_by_item["양파"]), 3)
        self.assertEqual(selections[0].candidate.product_id, "5438108496:양파")
        self.assertIn("리뷰 98,214개", selections[0].selection_reason)

    def test_sqlite_context_store_reads_prior_purchase_and_session_tables(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:db-user",
            chat_id="db-chat",
            raw_text="휴지 담아줘",
            request_id="request-db-1",
            items=[RequestedItem(name="휴지")],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "selection_context.sqlite3"
            self._create_context_database(database_path)

            context_store = SQLiteSelectionContextStore(database_path=str(database_path))
            context = context_store.load(request)
            selected = select_best_product(
                request.items[0],
                [
                    candidate(product_id="repeat-choice", name="휴지", price_krw=13900, rating=4.6, review_count=920),
                    candidate(product_id="avoid-choice", name="휴지", price_krw=12600, rating=4.7, review_count=1800),
                    candidate(product_id="neutral", name="휴지", price_krw=14600, rating=4.7, review_count=870),
                ],
                context=context,
            )

        self.assertEqual(len(context.prior_purchases), 1)
        self.assertEqual(len(context.recent_session_signals), 1)
        self.assertEqual(selected.candidate.product_id, "repeat-choice")
        self.assertIn("이전 구매 이력 3회", selected.selection_reason)

    @staticmethod
    def _create_context_database(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE prior_purchases (
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    purchase_count INTEGER NOT NULL,
                    last_purchased_at TEXT,
                    satisfaction_rating REAL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE recent_session_signals (
                    user_id TEXT NOT NULL,
                    request_id TEXT,
                    product_id TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    noted_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO prior_purchases (
                    user_id, product_id, product_name, purchase_count, last_purchased_at, satisfaction_rating
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "telegram:db-user",
                    "repeat-choice",
                    "휴지 재구매",
                    3,
                    "2026-03-10T08:00:00+09:00",
                    4.9,
                ),
            )
            connection.execute(
                """
                INSERT INTO recent_session_signals (
                    user_id, request_id, product_id, signal, noted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "telegram:db-user",
                    "request-db-1",
                    "avoid-choice",
                    "avoid",
                    "2026-03-11T08:30:00+09:00",
                ),
            )
            connection.commit()


def product_candidate_to_purchase_record(
    *,
    product_id: str,
    product_name: str,
    purchase_count: int,
    satisfaction_rating: float,
):
    from coupang_cart_agent.contracts import PriorPurchaseRecord

    return PriorPurchaseRecord(
        product_id=product_id,
        product_name=product_name,
        purchase_count=purchase_count,
        satisfaction_rating=satisfaction_rating,
    )


def session_signal(*, product_id: str, signal: str):
    from coupang_cart_agent.contracts import SessionSelectionSignal

    return SessionSelectionSignal(product_id=product_id, signal=signal)


class CandidateSourceNormalizationTests(unittest.TestCase):
    def test_product_candidate_from_record_normalizes_collector_shapes(self) -> None:
        candidate_record = product_candidate_from_record(
            {
                "productId": "p-1",
                "title": "  양파  ",
                "salesPrice": "12,300",
                "ratingAverage": "4.8",
                "ratingCount": "912",
                "productUrl": " https://www.coupang.com/vp/products/p-1 ",
                "vendorName": " 산지직송 ",
                "badgeNames": [" 로켓프레시 ", ""],
            }
        )

        self.assertEqual(candidate_record.product_id, "p-1")
        self.assertEqual(candidate_record.name, "양파")
        self.assertEqual(candidate_record.price_krw, 12300)
        self.assertEqual(candidate_record.rating, 4.8)
        self.assertEqual(candidate_record.review_count, 912)
        self.assertEqual(candidate_record.vendor, "산지직송")
        self.assertEqual(candidate_record.badges, ["로켓프레시"])


if __name__ == "__main__":
    unittest.main()
