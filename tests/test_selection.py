from __future__ import annotations

import unittest

from coupang_cart_agent.contracts import ProductCandidate, RequestedItem, ShoppingRequest
from coupang_cart_agent.selection import HeuristicProductSelectionService, select_best_product


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
        self.assertIn("rating 4.8/5", selected.selection_reason)
        self.assertIn("3,200 reviews", selected.selection_reason)

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


if __name__ == "__main__":
    unittest.main()
