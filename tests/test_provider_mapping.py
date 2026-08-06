import unittest
from unittest.mock import patch

from provider_checkout import PROVIDER_DEFAULTS, stripe_payment_method_type, stripe_to_provider


class ProviderMappingTests(unittest.TestCase):
    def test_gcash_uses_stripe_external_gcash_method_name(self):
        self.assertEqual(stripe_payment_method_type("gcash"), "external_gcash")

    def test_kakao_uses_stripe_kakao_pay_method_name(self):
        self.assertEqual(stripe_payment_method_type("kakao"), "kakao_pay")

    def test_kakao_default_region_is_krw(self):
        self.assertEqual(PROVIDER_DEFAULTS["kakao"], {"country": "KR", "currency": "KRW"})

    def test_existing_provider_names_are_unchanged(self):
        for provider in ("pix", "upi", "ideal", "twint", "momo"):
            with self.subTest(provider=provider):
                self.assertEqual(stripe_payment_method_type(provider), provider)

    def test_external_gcash_nonzero_promo_returns_checkout_link(self):
        logs = []
        ctx = {
            "payment_method_types": ["card", "external_gcash"],
            "checkout_amount": 110000,
            "currency": "php",
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_123",
        }

        with patch("provider_checkout.sc.init_checkout", return_value=({}, "v1", ctx)), patch(
            "provider_checkout.sc.fetch_elements_session"
        ), patch("provider_checkout.sc.update_tax_region"):
            result = stripe_to_provider(
                object(),
                "cs_live_123",
                "gcash",
                billing={"address": {"country": "PH"}},
                country="PH",
                stage1={
                    "publishable_key": "pk_live_test",
                    "processor_entity": "openai_llc",
                },
                require_zero_due=True,
                log=logs.append,
            )

        self.assertFalse(result["promo_applied"])
        self.assertEqual(result["checkout_amount"], 110000)
        self.assertEqual(result["payment_method_type"], "external_gcash")
        self.assertIn("redirect_pm_type=external_gcash", result["provider_redirect_url"])
        self.assertIn("优惠未生效", "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
