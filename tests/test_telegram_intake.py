from __future__ import annotations

import json
import unittest

from coupang_cart_agent.telegram_intake import (
    TelegramBotApiClient,
    TelegramIntakeError,
    TelegramPollingIntakeService,
)


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class TelegramIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TelegramPollingIntakeService()

    def test_parse_message_structures_quantity_and_name(self) -> None:
        request = self.service.parse_message(
            user_id="telegram:1",
            chat_id="chat-1",
            text="콜라 제로 355ml 2개 담아줘",
        )

        self.assertEqual(request.items[0].name, "콜라 제로 355ml")
        self.assertEqual(request.items[0].quantity, 2)
        self.assertEqual(request.items[0].constraints, [])

    def test_parse_message_structures_constraints_and_budget(self) -> None:
        request = self.service.parse_message(
            user_id="telegram:2",
            chat_id="chat-2",
            text="삼다수 2L 1박스 옵션: 무라벨, 빠른배송 20000원 이하 담아줘",
        )

        item = request.items[0]
        self.assertEqual(item.name, "삼다수 2L")
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.constraints, ["무라벨", "빠른배송"])
        self.assertEqual(item.max_price_krw, 20000)

    def test_parse_message_supports_multiple_items(self) -> None:
        request = self.service.parse_message(
            user_id="telegram:3",
            chat_id="chat-3",
            text="오트밀 2개\n두유 1팩 옵션: 무가당 담아줘",
        )

        self.assertEqual(len(request.items), 2)
        self.assertEqual(request.items[0].name, "오트밀")
        self.assertEqual(request.items[0].quantity, 2)
        self.assertEqual(request.items[1].name, "두유")
        self.assertEqual(request.items[1].quantity, 1)
        self.assertEqual(request.items[1].constraints, ["무가당"])

    def test_parse_message_requires_damajwo_suffix(self) -> None:
        with self.assertRaises(TelegramIntakeError) as context:
            self.service.parse_message(
                user_id="telegram:4",
                chat_id="chat-4",
                text="콜라 제로 2개",
            )

        self.assertIn("담아줘", str(context.exception))

    def test_parse_message_requires_product_name(self) -> None:
        with self.assertRaises(TelegramIntakeError) as context:
            self.service.parse_message(
                user_id="telegram:5",
                chat_id="chat-5",
                text="담아줘",
            )

        self.assertIn("상품명", str(context.exception))

    def test_handle_update_converts_telegram_payload_into_request(self) -> None:
        result = self.service.handle_update(
            {
                "update_id": 1001,
                "message": {
                    "message_id": 10,
                    "from": {"id": 321},
                    "chat": {"id": 654},
                    "text": "생수 6개 담아줘",
                },
            }
        )

        self.assertIsNone(result.error_message)
        self.assertIsNotNone(result.request)
        assert result.request is not None
        self.assertEqual(result.request.user_id, "telegram:321")
        self.assertEqual(result.request.chat_id, "654")
        self.assertEqual(result.request.request_id, "telegram-update-1001")
        self.assertEqual(result.request.items[0].quantity, 6)

    def test_handle_update_returns_user_facing_error_for_non_text_message(self) -> None:
        result = self.service.handle_update(
            {
                "update_id": 1002,
                "message": {
                    "message_id": 11,
                    "from": {"id": 111},
                    "chat": {"id": 222},
                    "photo": [{"file_id": "abc"}],
                },
            }
        )

        self.assertIsNone(result.request)
        self.assertIsNotNone(result.error_message)
        assert result.error_message is not None
        self.assertIn("텍스트 메시지", result.error_message)

    def test_poll_once_fetches_updates_through_bot_api(self) -> None:
        captured_request: dict[str, object] = {}

        def fake_opener(request) -> _FakeHttpResponse:
            captured_request["url"] = request.full_url
            captured_request["body"] = request.data.decode("utf-8")
            return _FakeHttpResponse(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1003,
                            "message": {
                                "message_id": 12,
                                "from": {"id": 777},
                                "chat": {"id": 888},
                                "text": "휴지 3개 담아줘",
                            },
                        }
                    ],
                }
            )

        client = TelegramBotApiClient(token="test-token", opener=fake_opener)
        service = TelegramPollingIntakeService(client)

        results = service.poll_once(offset=44, timeout=5)

        self.assertEqual(len(results), 1)
        self.assertIn("/bottest-token/getUpdates", captured_request["url"])
        self.assertIn("offset=44", captured_request["body"])
        self.assertIn("timeout=5", captured_request["body"])
        self.assertEqual(results[0].request.items[0].name, "휴지")


if __name__ == "__main__":
    unittest.main()
