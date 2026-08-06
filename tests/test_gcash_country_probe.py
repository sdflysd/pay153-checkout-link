import unittest

from tools.gcash_country_probe import (
    method_snapshot,
    proxy_for_country,
    proxy_label,
    summarize_stripe_state,
)


class GCashCountryProbeTests(unittest.TestCase):
    def test_proxy_for_country_rewrites_cliproxy_region_and_sid(self):
        proxy = (
            "us2.cliproxy.io:3010:"
            "user-region-PH-sid-OLD123-t-60:secret"
        )

        routed = proxy_for_country(proxy, "SG")

        self.assertIn("region-SG", routed)
        self.assertNotIn("sid-OLD123", routed)

    def test_proxy_label_redacts_credentials(self):
        proxy = (
            "us2.cliproxy.io:3010:"
            "user-region-PH-sid-OLD123-t-60:secret"
        )

        label = proxy_label(proxy)

        self.assertIn("region=PH", label)
        self.assertIn("auth=redacted", label)
        self.assertNotIn("secret", label)
        self.assertNotIn("OLD123", label)

    def test_method_snapshot_detects_gcash_payment_method_specs(self):
        payload = {
            "checkout_session": {
                "payment_method_specs": [
                    {"type": "card", "display_name": "Card"},
                    {"type": "gcash", "display_name": "GCash"},
                ]
            }
        }

        snapshot = method_snapshot(payload)

        self.assertTrue(snapshot["has_gcash"])
        self.assertEqual(snapshot["selected_gcash_method"], "gcash")

    def test_stripe_summary_preserves_external_gcash_name(self):
        snapshot = summarize_stripe_state(
            {},
            {"payment_method_specs": [{"type": "card"}, {"type": "external_gcash"}]},
            {"payment_method_types": ["card"], "checkout_amount": 0, "currency": "php"},
        )

        self.assertTrue(snapshot["has_gcash"])
        self.assertEqual(snapshot["selected_gcash_method"], "external_gcash")


if __name__ == "__main__":
    unittest.main()
