from __future__ import annotations

import unittest

from coupang_cart_agent.cart_verification import DeterministicCartVerifier
from coupang_cart_agent.contracts import (
    BrowserObservation,
    CartAddFailureReason,
    ObservedCartItem,
    ProductCandidate,
    SelectedProduct,
)


class CartVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = DeterministicCartVerifier()

    def test_verifier_rejects_false_success_when_cart_contains_different_item(self) -> None:
        selection = SelectedProduct(
            request_item_name="우유",
            candidate=ProductCandidate(
                product_id="MILK-1",
                name="서울우유 1L",
                price_krw=3200,
                rating=4.8,
                review_count=1200,
                product_url="https://www.coupang.com/vp/products/MILK-1",
            ),
            quantity=1,
            selection_reason="Best match.",
            score=10.0,
        )
        observation = BrowserObservation(
            step_index=0,
            url="https://cart.coupang.com/cartView.pang",
            title="쿠팡 장바구니",
            page_kind="browse",
            body_text_excerpt="양파 추천 수량 1",
            accessibility_lines=["link:양파 추천", "button:수량 1"],
            screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
            cart_items=[
                ObservedCartItem(
                    name="양파 추천",
                    quantity=1,
                    quantity_text="1개",
                )
            ],
            cart_count=1,
        )

        decision = self.verifier.verify(
            selection=selection,
            observation=observation,
            cart_count_before=0,
            cart_count_after=1,
        )

        self.assertFalse(decision.success)
        self.assertEqual(decision.failure_reason, CartAddFailureReason.MANUAL_REVIEW_REQUIRED)
        self.assertTrue(decision.evidence["cart_observation"]["has_screenshot"])

    def test_verifier_accepts_generic_request_when_cart_item_semantically_matches(self) -> None:
        selection = SelectedProduct(
            request_item_name="양파",
            candidate=ProductCandidate(
                product_id="ONION-1",
                name="곰곰 국내산 양파 3kg",
                price_krw=8900,
                rating=4.7,
                review_count=1532,
                product_url="https://www.coupang.com/vp/products/ONION-1",
            ),
            quantity=1,
            selection_reason="Best match.",
            score=9.8,
        )
        observation = BrowserObservation(
            step_index=0,
            url="https://cart.coupang.com/cartView.pang",
            title="쿠팡 장바구니",
            page_kind="browse",
            body_text_excerpt="곰곰 국내산 양파 3kg 수량 1",
            accessibility_lines=["link:곰곰 국내산 양파 3kg", "button:수량 1"],
            screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
            cart_items=[
                ObservedCartItem(
                    name="곰곰 국내산 양파 3kg",
                    quantity=1,
                    quantity_text="1개",
                    package_summary="3kg",
                )
            ],
            cart_count=1,
        )

        decision = self.verifier.verify(
            selection=selection,
            observation=observation,
            cart_count_before=0,
            cart_count_after=1,
        )

        self.assertTrue(decision.success)
        self.assertEqual(decision.matched_item_name, "곰곰 국내산 양파 3kg")


if __name__ == "__main__":
    unittest.main()
