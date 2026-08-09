import unittest
import base64
import json
from unittest.mock import patch

from app import (
    apply_chatgpt_cookies,
    chatgpt_impersonate,
    chatgpt_cookie_did,
    chatgpt_cookie_names,
    chatgpt_user_agent_for_session,
    checkout_payload,
    checkout_redirect_url_from_payload,
    checkout_amount_verification,
    checkout_proxy_for_provider,
    confirm_custom_checkout_method,
    create_stripe_confirmation_token,
    custom_checkout_billing_is_complete,
    custom_checkout_confirm_return_url,
    custom_checkout_confirm_body,
    custom_checkout_method_is_external,
    custom_checkout_method_is_custom,
    select_custom_checkout_method_from_states,
    default_entry_proxy_country,
    default_exit_proxy_country,
    extract_custom_checkout_methods,
    extract_access_token,
    gcash_preselected_checkout_url,
    gcash_page_fallback_allowed,
    gcash_done_text,
    has_chatgpt_session_cookie,
    is_non_retryable_checkout_error,
    merge_chrome_cdp_credentials,
    promo_proxy_for_provider,
    ProxySentinel,
    SENTINEL_DEFAULT_TELEMETRY,
    select_custom_checkout_method,
    start_custom_checkout_method,
)


def _b64url(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_access_token():
    return ".".join(
        [
            _b64url({"alg": "none", "typ": "JWT"}),
            _b64url({
                "email": "tester@example.com",
                "exp": 4102444800,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-test"},
            }),
            "sig",
        ]
    )


class DummyCookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, domain=None):
        self.values[name] = value

    def items(self):
        return self.values.items()


class DummySession:
    def __init__(self):
        self.headers = {}
        self.cookies = DummyCookies()


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, separators=(",", ":"))

    def json(self):
        return self._payload


class DummyPostingSession(DummySession):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return self.response


class FlakyPostingSession(DummySession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def fake_sentinel_headers(*_args, **_kwargs):
    return {"OpenAI-Sentinel-Token": '"sen"'}


class CheckoutProxyRoutingTests(unittest.TestCase):
    def test_custom_confirm_blocked_is_not_retryable(self):
        message = (
            "CUSTOM_CONFIRM_BLOCKED: GCash 支付方式确认被上游拦截；"
            "{\"status\":\"blocked\"}；Chrome fallback unavailable: connect ECONNREFUSED 127.0.0.1:9222"
        )

        self.assertTrue(is_non_retryable_checkout_error(message))

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

    def test_gcash_default_checkout_country_matches_billing_country(self):
        self.assertEqual(default_entry_proxy_country("gcash", "PH"), "PH")
        self.assertEqual(default_exit_proxy_country("gcash", "PH", "", True), "VN")

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

    def test_gcash_uses_redirect_checkout_to_expose_stripe_methods(self):
        options = {
            "plan": "plus",
            "link_type": "gcash",
            "country": "PH",
            "currency": "PHP",
            "checkout_country": "PH",
            "checkout_currency": "PHP",
            "use_promo": True,
            "promo_campaign": "plus-1-month-free",
        }

        payload = checkout_payload(options, {})

        self.assertEqual(payload["checkout_ui_mode"], "redirect")
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

    def test_gcash_method_selection_checks_nested_update_payload(self):
        update_payload = {
            "success": True,
            "checkout_session": {
                "payment_method_types": ["card", "gcash"],
                "custom_payment_methods": [
                    {"id": "cpmt_gcash", "display_name": "GCash"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", {}, update_payload),
            "cpmt_gcash",
        )

    def test_gcash_method_selection_rejects_card_and_link_only(self):
        update_payload = {
            "success": True,
            "checkout_session": {
                "payment_method_types": ["card", "link"],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", update_payload),
            "",
        )

    def test_gcash_method_selection_accepts_single_anonymous_cpmt_with_card_link(self):
        payload = {
            "checkout_session": {
                "payment_method_types": ["link", "card"],
                "custom_payment_methods": [
                    {"id": "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ", "type": "custom_payment_method"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", payload),
            "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ",
        )

    def test_gcash_method_selection_rejects_ambiguous_anonymous_cpmt_methods(self):
        payload = {
            "checkout_session": {
                "payment_method_types": ["link", "card"],
                "custom_payment_methods": [
                    {"id": "cpmt_first", "type": "custom_payment_method"},
                    {"id": "cpmt_second", "type": "custom_payment_method"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", payload),
            "",
        )

    def test_card_link_only_summary_remains_diagnostic_for_page_fallback(self):
        payload = {
            "checkout_session": {
                "payment_method_types": ["card", "link"],
            },
        }

        self.assertEqual(select_custom_checkout_method_from_states("gcash", payload), "")

    def test_gcash_method_selection_accepts_external_method_fields(self):
        payload = {
            "checkout_session": {
                "externalPaymentMethods": [
                    {"id": "cpmt_ph_gcash", "type": "external_gcash", "display_name": "GCash"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", payload),
            "cpmt_ph_gcash",
        )

    def test_gcash_method_selection_accepts_external_gcash_type(self):
        payload = {
            "checkout_session": {
                "externalPaymentMethods": [
                    {"id": "epm_unstable", "type": "external_gcash", "display_name": "GCash"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", payload),
            "external_gcash",
        )

    def test_gcash_method_selection_accepts_payment_method_specs(self):
        payload = {
            "checkout_session": {
                "payment_method_specs": [
                    {"type": "card", "display_name": "Card"},
                    {"type": "gcash", "display_name": "GCash"},
                ],
            },
        }

        self.assertEqual(
            select_custom_checkout_method_from_states("gcash", payload),
            "gcash",
        )

    def test_gcash_nonzero_promo_is_reported_without_blocking_page_result(self):
        self.assertEqual(checkout_amount_verification(110000), "nonzero")
        self.assertEqual(checkout_amount_verification(0), "verified_zero")
        self.assertEqual(checkout_amount_verification(None), "pending")
        self.assertIn(
            "优惠未生效",
            gcash_done_text("GCash Checkout 页面已生成，请在页面选择 GCash", True, "nonzero"),
        )
        self.assertNotIn(
            "优惠未生效",
            gcash_done_text("GCash Checkout 页面已生成，请在页面选择 GCash", True, "verified_zero"),
        )

    def test_gcash_preselected_checkout_url_adds_external_method_params(self):
        url = gcash_preselected_checkout_url(
            "https://chatgpt.com/checkout/openai_llc/oaics_123?existing=1"
        )

        self.assertIn("existing=1", url)
        self.assertIn("redirect_pm_type=external_gcash", url)
        self.assertIn("ui_mode=custom", url)
        self.assertIn("lid=", url)

    def test_gcash_page_fallback_is_opt_in(self):
        self.assertFalse(gcash_page_fallback_allowed({}))
        self.assertFalse(gcash_page_fallback_allowed({"allow_gcash_page_fallback": False}))
        self.assertTrue(gcash_page_fallback_allowed({"allow_gcash_page_fallback": True}))
        self.assertTrue(gcash_page_fallback_allowed({"allow_gcash_page_fallback": "1"}))

    def test_start_external_gcash_sends_external_aliases(self):
        session = DummyPostingSession(DummyResponse(200, {
            "status": "requires_action",
            "next_action": {"url": "https://gcash.example/redirect"},
        }))

        payload = start_custom_checkout_method(
            session,
            fake_access_token(),
            "oaics_123",
            "openai_llc",
            "external_gcash",
            "device-id",
        )

        self.assertEqual(payload["next_action"]["url"], "https://gcash.example/redirect")
        request_body = session.posts[0]["json"]
        self.assertEqual(request_body["custom_payment_method_type_id"], "external_gcash")
        self.assertEqual(request_body["external_payment_method_type"], "external_gcash")
        self.assertEqual(request_body["externalPaymentMethodType"], "external_gcash")
        self.assertEqual(request_body["selected_payment_method_type"], "external_gcash")
        self.assertEqual(request_body["selectedPaymentMethodType"], "external_gcash")

    def test_start_custom_gcash_accepts_nested_redirect_url(self):
        session = DummyPostingSession(DummyResponse(200, {
            "status": "requires_action",
            "next_action": {
                "redirect_to_url": {"url": "https://gcash.example/nested"},
            },
        }))

        payload = start_custom_checkout_method(
            session,
            fake_access_token(),
            "oaics_123",
            "openai_llc",
            "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ",
            "device-id",
        )

        self.assertEqual(
            checkout_redirect_url_from_payload(payload),
            "https://gcash.example/nested",
        )
        request_body = session.posts[0]["json"]
        self.assertEqual(
            request_body["custom_payment_method_type_id"],
            "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ",
        )

    def test_kakao_pay_method_type_is_not_custom_method(self):
        self.assertFalse(custom_checkout_method_is_custom("kakao_pay"))
        self.assertTrue(custom_checkout_method_is_custom("cpmt_kakao"))
        self.assertFalse(custom_checkout_method_is_external("cpmt_kakao"))
        self.assertTrue(custom_checkout_method_is_external("external_gcash"))

    def test_external_gcash_confirm_body_uses_external_payment_method(self):
        body = custom_checkout_confirm_body("oaics_123", "external_gcash")

        self.assertEqual(body["checkout_session_id"], "oaics_123")
        self.assertEqual(body["selected_payment_method_type"], "external_gcash")
        self.assertEqual(body["selectedPaymentMethodType"], "external_gcash")
        self.assertEqual(body["type"], "external_payment_method")
        self.assertEqual(body["external_payment_method_type"], "external_gcash")
        self.assertEqual(body["externalPaymentMethodType"], "external_gcash")

    def test_confirm_body_includes_confirmation_token_for_native_kakao(self):
        body = custom_checkout_confirm_body("oaics_123", "kakao_pay", "ctoken_123")
        self.assertEqual(body["checkout_session_id"], "oaics_123")
        self.assertEqual(body["selected_payment_method_type"], "kakao_pay")
        self.assertEqual(body["selectedPaymentMethodType"], "kakao_pay")
        self.assertEqual(body["type"], "confirmation_token")
        self.assertEqual(body["confirm_token"], "ctoken_123")
        self.assertEqual(body["confirmToken"], "ctoken_123")
        self.assertEqual(body["confirmation_token"], "ctoken_123")
        self.assertNotIn("confirmation_token_id", body)

    def test_confirm_body_can_include_billing_details_for_native_kakao(self):
        billing = {
            "name": "Tester",
            "email": "tester@example.com",
            "address": {
                "country": "KR",
                "line1": "30 Eulji-ro",
                "line2": "",
                "city": "Seoul",
                "state": "Seoul",
                "postal_code": "04533",
            },
        }

        body = custom_checkout_confirm_body(
            "oaics_123",
            "kakao_pay",
            "ctoken_123",
            billing,
        )

        self.assertEqual(body["billing_details"]["name"], "Tester")
        self.assertEqual(body["billing_details"]["address"]["country"], "KR")
        self.assertEqual(body["billing_details"]["address"]["postal_code"], "04533")
        self.assertEqual(
            body["payment_method_data"]["billing_details"],
            body["billing_details"],
        )
        self.assertEqual(body["billingAddress"]["address"]["line1"], "30 Eulji-ro")

    def test_kakao_billing_requires_state(self):
        billing = {
            "name": "Tester",
            "address": {
                "country": "KR",
                "line1": "30 Eulji-ro",
                "city": "Seoul",
                "postal_code": "04533",
            },
        }

        self.assertFalse(custom_checkout_billing_is_complete(billing))
        billing["address"]["state"] = "Seoul"
        self.assertTrue(custom_checkout_billing_is_complete(billing))

    def test_blocked_confirm_can_succeed_through_chrome_cdp_fallback(self):
        session = DummyPostingSession(DummyResponse(200, {"status": "blocked"}))
        session.headers["Cookie"] = "__Secure-next-auth.session-token=session-token; oai-did=did"
        logs = []
        fallback_payload = {
            "_helper_ok": True,
            "status": 200,
            "text": '{"status":"success"}',
            "json": {"status": "success", "next_action": {"url": "https://pay.example/next"}},
        }

        with patch.dict("os.environ", {"PAY153_CHROME_CDP_CONFIRM_PRIMARY": "0"}), patch(
            "app.sentinel_headers", new=fake_sentinel_headers
        ), patch("app.chrome_cdp_fetch_json", return_value=fallback_payload) as fallback:
            payload = confirm_custom_checkout_method(
                session,
                fake_access_token(),
                "oaics_123",
                "openai_ie",
                "kakao_pay",
                "",
                "device-id",
                "did",
                method_name="Kakao Pay",
                confirmation_token="ctoken_123",
                log=logs.append,
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(fallback.call_count, 1)
        fallback_kwargs = fallback.call_args.kwargs
        self.assertEqual(fallback_kwargs["body"]["confirm_token"], "ctoken_123")
        self.assertEqual(fallback_kwargs["select_payment_method"], "kakao_pay")
        self.assertEqual(fallback_kwargs["page_url"], "https://chatgpt.com/checkout/openai_ie/oaics_123")
        self.assertIn("Chrome 9222 confirm fallback: HTTP 200", "\n".join(logs))

    def test_native_kakao_confirm_prefers_chrome_cdp_primary(self):
        session = DummyPostingSession(DummyResponse(200, {"status": "blocked"}))
        session.headers["Cookie"] = "__Secure-next-auth.session-token=session-token; oai-did=did"
        logs = []
        primary_payload = {
            "_helper_ok": True,
            "status": 200,
            "text": '{"status":"success"}',
            "json": {"status": "success", "next_action": {"url": "https://pay.example/next"}},
        }

        with patch.dict("os.environ", {"PAY153_CHROME_CDP_CONFIRM_PRIMARY": ""}), patch(
            "app.sentinel_headers", new=fake_sentinel_headers
        ), patch("app.chrome_cdp_fetch_json", return_value=primary_payload) as primary:
            payload = confirm_custom_checkout_method(
                session,
                fake_access_token(),
                "oaics_123",
                "openai_ie",
                "kakao_pay",
                "",
                "device-id",
                "did",
                method_name="Kakao Pay",
                confirmation_token="ctoken_123",
                log=logs.append,
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(session.posts, [])
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(primary.call_args.kwargs["select_payment_method"], "kakao_pay")
        self.assertIn("Chrome 9222 confirm primary: HTTP 200", "\n".join(logs))

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

    def test_confirm_return_url_prefers_checkout_state_value(self):
        payload = {
            "checkout_session": {
                "confirm_return_url": (
                    "https://chatgpt.com/checkout/verify?"
                    "stripe_session_id=oaics_123&processor_entity=openai_llc&plan_type=plus"
                )
            }
        }

        self.assertEqual(
            custom_checkout_confirm_return_url(payload, "oaics_123", "openai_ie", "plus"),
            payload["checkout_session"]["confirm_return_url"],
        )

    def test_confirm_return_url_defaults_to_verify_route(self):
        self.assertEqual(
            custom_checkout_confirm_return_url({}, "oaics_123", "openai_ie", "plus"),
            "https://chatgpt.com/checkout/verify?stripe_session_id=oaics_123&processor_entity=openai_ie&plan_type=plus",
        )

    def test_stripe_confirmation_token_retries_transient_post_error(self):
        session = FlakyPostingSession([
            OSError("connection reset"),
            DummyResponse(200, {"id": "ctoken_retry"}),
        ])
        billing = {
            "name": "Tester",
            "email": "tester@example.com",
            "address": {
                "country": "KR",
                "line1": "30 Eulji-ro",
                "city": "Seoul",
                "state": "Seoul",
                "postal_code": "04533",
            },
        }
        logs = []

        token = create_stripe_confirmation_token(
            session,
            "pk_live_test",
            "kakao_pay",
            billing,
            "https://chatgpt.com/checkout/verify?stripe_session_id=oaics_123&processor_entity=openai_ie&plan_type=plus",
            logs.append,
        )

        self.assertEqual(token, "ctoken_retry")
        self.assertEqual(len(session.posts), 2)
        self.assertIn("网络重试", "\n".join(logs))

    def test_checkout_sentinel_uses_chatgpt_backend(self):
        provider = ProxySentinel(None, {"oai-did": "did"})
        self.assertEqual(provider.BACKEND_URL, "https://chatgpt.com/backend-api/sentinel/")
        self.assertEqual(
            provider.FRAME_REFERER,
            "https://chatgpt.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
        )
        self.assertEqual(SENTINEL_DEFAULT_TELEMETRY, "[1,null]")

    def test_session_json_collects_chatgpt_cookie_array(self):
        raw = json.dumps({
            "accessToken": fake_access_token(),
            "cookies": [
                {"name": "__Secure-next-auth.session-token", "value": "session-token"},
                {"name": "cf_clearance", "value": "cf-token"},
                {"name": "oai-did", "value": "cookie-did"},
                {"name": "unrelated", "value": "ignored"},
            ],
        })
        token, meta = extract_access_token(raw)

        self.assertEqual(token, fake_access_token())
        self.assertEqual(meta["email"], "tester@example.com")
        self.assertEqual(chatgpt_cookie_did(meta), "cookie-did")
        self.assertEqual(
            chatgpt_cookie_names(meta),
            ["__Secure-next-auth.session-token", "cf_clearance", "oai-did"],
        )

    def test_session_json_accepts_bare_session_cookie_token(self):
        raw = json.dumps({
            "accessToken": fake_access_token(),
            "session_cookie": "bare-next-auth-token",
        })
        _token, meta = extract_access_token(raw)

        self.assertIn("__Secure-next-auth.session-token", chatgpt_cookie_names(meta))

    def test_apply_chatgpt_cookies_preserves_session_and_overrides_did(self):
        raw = json.dumps({
            "accessToken": fake_access_token(),
            "chatgpt_session_cookie": (
                "__Secure-next-auth.session-token=session-token; "
                "cf_clearance=cf-token; oai-did=old-did; unrelated=ignored"
            ),
        })
        _token, meta = extract_access_token(raw)
        session = DummySession()

        header = apply_chatgpt_cookies(session, meta, "runtime-did")

        self.assertIn("__Secure-next-auth.session-token=session-token", header)
        self.assertIn("cf_clearance=cf-token", header)
        self.assertIn("oai-did=runtime-did", header)
        self.assertNotIn("old-did", header)
        self.assertNotIn("unrelated", header)
        self.assertEqual(session.cookies.values["oai-did"], "runtime-did")

    def test_chrome_cdp_snapshot_replaces_bare_token_with_browser_session(self):
        bare_token, bare_meta = extract_access_token(fake_access_token())
        browser_token = ".".join(
            [
                _b64url({"alg": "none", "typ": "JWT"}),
                _b64url({
                    "email": "browser@example.com",
                    "exp": 4102444800,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acc-browser"},
                }),
                "sig",
            ]
        )
        snapshot = {
            "ok": True,
            "accessToken": browser_token,
            "user": {"email": "browser@example.com"},
            "account": {"id": "acc-browser"},
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "language": "ko-KR",
            "cookies": {
                "__Secure-next-auth.session-token": "browser-session",
                "cf_clearance": "browser-cf",
                "oai-did": "browser-did",
            },
        }

        token, meta, changed = merge_chrome_cdp_credentials(bare_token, bare_meta, snapshot)

        self.assertTrue(changed)
        self.assertEqual(token, browser_token)
        self.assertTrue(has_chatgpt_session_cookie(meta))
        self.assertEqual(meta["email"], "browser@example.com")
        self.assertEqual(meta["account_id"], "acc-browser")
        self.assertEqual(chatgpt_cookie_did(meta), "browser-did")
        self.assertEqual(chatgpt_impersonate(meta), "chrome146")

        session = DummySession()
        apply_chatgpt_cookies(session, meta, "browser-did")
        self.assertIn("Chrome/151.0.0.0", chatgpt_user_agent_for_session(session))
        self.assertIn("ko-KR", session.headers["Accept-Language"])

    def test_chrome_cdp_snapshot_without_session_cookie_is_ignored(self):
        bare_token, bare_meta = extract_access_token(fake_access_token())
        token, meta, changed = merge_chrome_cdp_credentials(
            bare_token,
            bare_meta,
            {"ok": True, "accessToken": fake_access_token(), "cookies": {"oai-did": "only-did"}},
        )

        self.assertFalse(changed)
        self.assertEqual(token, bare_token)
        self.assertFalse(has_chatgpt_session_cookie(meta))


if __name__ == "__main__":
    unittest.main()
