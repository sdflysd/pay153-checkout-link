import unittest

from provider_checkout import PROVIDER_DEFAULTS, stripe_payment_method_type


class ProviderMappingTests(unittest.TestCase):
    def test_kakao_uses_stripe_kakao_pay_method_name(self):
        self.assertEqual(stripe_payment_method_type("kakao"), "kakao_pay")

    def test_kakao_default_region_is_krw(self):
        self.assertEqual(PROVIDER_DEFAULTS["kakao"], {"country": "KR", "currency": "KRW"})

    def test_existing_provider_names_are_unchanged(self):
        for provider in ("pix", "upi", "ideal", "twint", "momo"):
            with self.subTest(provider=provider):
                self.assertEqual(stripe_payment_method_type(provider), provider)


if __name__ == "__main__":
    unittest.main()
