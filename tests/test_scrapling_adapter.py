from __future__ import annotations

import unittest

from coupang_cart_agent.scrapling_adapter import ScraplingObservationAdapter


class ScraplingObservationAdapterTests(unittest.TestCase):
    def test_extract_uses_json_ld_products_on_search_pages(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "mainEntity": {
                  "@type": "ItemList",
                  "itemListElement": [
                    {
                      "@type": "ListItem",
                      "position": 1,
                      "item": {
                        "@type": "Product",
                        "name": "콘푸로스트 시리얼, 660g, 1개",
                        "url": "https://www.coupang.com/vp/products/CEREAL-660?itemId=1&vendorItemId=2",
                        "offers": {
                          "@type": "Offer",
                          "price": "6260",
                          "priceCurrency": "KRW",
                          "availability": "https://schema.org/InStock"
                        },
                        "aggregateRating": {
                          "@type": "AggregateRating",
                          "ratingValue": "4.9",
                          "reviewCount": "53466"
                        }
                      }
                    }
                  ]
                }
              }
            </script>
          </head>
          <body>
            <ul>
              <li>
                <a href="/vp/products/CEREAL-660?itemId=1&vendorItemId=2">
                  <span>콘푸로스트 시리얼, 660g, 1개</span>
                </a>
              </li>
            </ul>
            <a href="/np/campaigns/83">와우회원할인</a>
          </body>
        </html>
        """
        adapter = ScraplingObservationAdapter(storage_path=".artifacts/test-scrapling-observation.sqlite3")

        snapshot, hints = adapter.extract(
            url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
            html=html,
            body_text="시리얼 검색 결과 와우회원할인",
            viewport_state={"interactive_elements": ["a:와우회원할인"], "cart_ctas": []},
        )

        self.assertEqual(len(snapshot["observed_products"]), 1)
        self.assertEqual(snapshot["observed_products"][0]["name"], "콘푸로스트 시리얼, 660g, 1개")
        self.assertEqual(snapshot["observed_products"][0]["price_text"], "6260")
        self.assertIsNone(snapshot["purchase_blocked_reason"])
        self.assertIn(
            "https://www.coupang.com/vp/products/CEREAL-660?itemId=1&vendorItemId=2",
            hints["search_result_links"],
        )

    def test_extract_purchase_restriction_only_on_product_pages(self) -> None:
        html = "<html><body><h1>상품 상세</h1><button>장바구니 담기</button></body></html>"
        adapter = ScraplingObservationAdapter(storage_path=".artifacts/test-scrapling-observation.sqlite3")

        search_snapshot, _ = adapter.extract(
            url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
            html=html,
            body_text="와우회원할인 혜택",
            viewport_state={"interactive_elements": [], "cart_ctas": []},
        )
        product_snapshot, _ = adapter.extract(
            url="https://www.coupang.com/vp/products/CEREAL-1",
            html=html,
            body_text="로켓프레시 상품은 장바구니에 담을 수 없습니다.",
            viewport_state={"interactive_elements": [], "cart_ctas": []},
        )

        self.assertIsNone(search_snapshot["purchase_blocked_reason"])
        self.assertEqual(product_snapshot["purchase_blocked_reason"], "rocket_fresh_restriction")

    def test_extract_product_title_prefers_product_json_ld_over_generic_heading(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="오리온 미쯔블랙 시리얼, 360g, 1개 - 시리얼 | 쿠팡" />
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "오리온 미쯔블랙 시리얼, 360g, 1개",
                "url": "https://www.coupang.com/vp/products/CEREAL-1",
                "offers": {
                  "@type": "Offer",
                  "price": "4820",
                  "availability": "https://schema.org/InStock"
                },
                "aggregateRating": {
                  "@type": "AggregateRating",
                  "ratingValue": "5",
                  "reviewCount": "17384"
                }
              }
            </script>
          </head>
          <body>
            <h2>다른 고객이 함께 본 상품</h2>
          </body>
        </html>
        """
        adapter = ScraplingObservationAdapter(storage_path=".artifacts/test-scrapling-observation.sqlite3")

        snapshot, _ = adapter.extract(
            url="https://www.coupang.com/vp/products/CEREAL-1",
            html=html,
            body_text="오리온 미쯔블랙 시리얼, 360g, 1개 17,384개 상품평",
            viewport_state={"interactive_elements": [], "cart_ctas": []},
        )

        self.assertEqual(snapshot["selected_product_hint"]["name"], "오리온 미쯔블랙 시리얼, 360g, 1개")
        self.assertEqual(snapshot["selected_product_hint"]["price_text"], "4820")


if __name__ == "__main__":
    unittest.main()
