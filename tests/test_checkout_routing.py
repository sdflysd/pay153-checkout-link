import unittest

from app import (
    checkout_payload,
    checkout_redirect_url_from_payload,
    checkout_proxy_for_provider,
    custom_checkout_confirm_body,
    custom_checkout_method_is_custom,
    default_entry_proxy_country,
    default_exit_proxy_country,
    extract_custom_checkout_methods,
    promo_proxy_for_provider,
    ProxySentinel,
    SENTINEL_DEFAULT_TELEMETRY,
    select_custom_checkout_method,
)


class CheckoutProxyRoutingTests(unittest.TestCase):
    def test_kakao_checkout_uses_exit_proxy(self):
        self.assertEqual(
            checkout_proxy_for_provider("kakao", "POOL1_VN", "POOL2_KR"),
            "POOL2_KR",
        )

    def test_kakao_promo_uses_entry_proxy(self):
        self.assertEqual(
            promo_proxy_for_provider("kakao", "POOL1_VN", "POOL2_KR"),
            "POOL1_VN",
        )

    def test_kakao_default_proxy_countries(self):
        self.assertEqual(default_entry_proxy_country("kakao", "KR"), "VN")
        self.assertEqual(default_exit_proxy_country("kakao", "KR", "", True), "KR")

    def test_exit_proxy_checkout_providers(self):
        for provider in ("paypal", "upi", "ideal", "twint"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    checkout_proxy_for_provider(provider, "POOL1", "POOL2"),
                    "POOL2",
                )

    def test_single_chain_checkout_providers(self):
        for provider in ("hosted", "pix", "momo", "gcash"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    checkout_proxy_for_provider(provider, "POOL1", "POOL2"),
                    "POOL1",
                )

    def test_kakao_promo_is_not_attached_on_checkout_create(self):
        options = {
            "plan": "plus",
            "link_type": "kakao",
            "country": "KR",
            "currency": "KRW",
            "checkout_country": "KR",
            "checkout_currency": "KRW",
            "use_promo": True,
            "promo_campaign": "plus-1-month-free",
        }
        payload = checkout_payload(options, {})
        self.assertNotIn("promo_campaign", payload)

    def test_kakao_custom_method_selection_prefers_kakao(self):
        methods = [
            {"id": "cpmt_card", "display_name": "Local card"},
            {"id": "cpmt_naver", "display_name": "Naver Pay"},
            {"id": "cpmt_kakao", "display_name": "Kakao Pay", "type": "kakao_pay"},
        ]
        self.assertEqual(select_custom_checkout_method(methods, "kakao"), "cpmt_kakao")

    def test_kakao_custom_method_selection_does_not_pick_naver_or_card(self):
        methods = [
            {"id": "cpmt_card", "display_name": "Local card"},
            {"id": "cpmt_naver", "display_name": "Naver Pay"},
        ]
        self.assertEqual(select_custom_checkout_method(methods, "kakao"), "")

    def test_kakao_custom_method_selection_accepts_method_type_strings(self):
        methods = ["card", "kakao_pay", "naver_pay"]
        self.assertEqual(select_custom_checkout_method(methods, "kakao"), "kakao_pay")

    def test_kakao_custom_method_selection_accepts_nested_payment_method_types(self):
        payload = {
            "checkout_session": {
                "payment_method_types": ["card", "kakao_pay", "naver_pay"],
            },
        }
        self.assertEqual(select_custom_checkout_method(payload, "kakao"), "kakao_pay")

    def test_kakao_custom_method_selection_accepts_korean_label(self):
        methods = [
            {"id": "nicepay_kakao", "display_name": "카카오페이"},
            {"id": "naver_pay", "display_name": "네이버페이"},
        ]
        self.assertEqual(select_custom_checkout_method(methods, "kakao"), "nicepay_kakao")

    def test_custom_method_extraction_collects_nested_methods(self):
        payload = {
            "checkout_session": {
                "available_payment_methods": [
                    {"id": "card", "display_name": "Card"},
                    {"id": "kakao_pay", "display_name": "Kakao Pay"},
                ],
            },
        }
        identifiers = [str(item.get("id") or item) for item in extract_custom_checkout_methods(payload)]
        self.assertIn("kakao_pay", identifiers)

    def test_kakao_pay_method_type_is_not_custom_method(self):
        self.assertFalse(custom_checkout_method_is_custom("kakao_pay"))
        self.assertTrue(custom_checkout_method_is_custom("cpmt_kakao"))

    def test_confirm_body_includes_confirmation_token_for_native_kakao(self):
        body = custom_checkout_confirm_body("oaics_123", "kakao_pay", "ctoken_123")
        self.assertEqual(body["checkout_session_id"], "oaics_123")
        self.assertEqual(body["selected_payment_method_type"], "kakao_pay")
        self.assertEqual(body["confirm_token"], "ctoken_123")
        self.assertNotIn("confirmation_token", body)
        self.assertNotIn("confirmation_token_id", body)

    def test_checkout_redirect_url_from_payload_finds_nested_kakao_url(self):
        payload = {
            "status": "success",
            "next_action": {
                "redirect_to_url": {
                    "url": "https://web.nicepay.co.kr/v3/v3Payment.jsp?x=1",
                }
            },
        }
        self.assertEqual(
            checkout_redirect_url_from_payload(payload),
            "https://web.nicepay.co.kr/v3/v3Payment.jsp?x=1",
        )

    def test_checkout_sentinel_uses_chatgpt_backend(self):
        provider = ProxySentinel(None, {"oai-did": "did"})
        self.assertEqual(provider.BACKEND_URL, "https://chatgpt.com/backend-api/sentinel/")
        self.assertEqual(
            provider.FRAME_REFERER,
            "https://chatgpt.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
        )
        self.assertEqual(SENTINEL_DEFAULT_TELEMETRY, "[1,null]")


if __name__ == "__main__":
    unittest.main()
