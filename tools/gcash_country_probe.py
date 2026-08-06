#!/usr/bin/env python3
"""Probe which OpenAI Checkout country/proxy combinations expose GCash.

This is intentionally read-only for payments: it creates Checkout sessions and
optionally applies the promo/update and tax steps, but it never confirms or
starts a payment method.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as pay_app  # noqa: E402
import stripe_checkout as sc  # noqa: E402
from ph_short_extractor import rotate_proxy_session  # noqa: E402
from provider_checkout import default_billing  # noqa: E402


DEFAULT_CHECKOUT_COUNTRIES = ["PH"]
DEFAULT_PROXY_COUNTRIES = ["PH", "SG", "MY", "ID", "TH", "VN", "US"]
DEFAULT_UI_MODES = ["redirect", "custom"]
DEFAULT_PROFILE_COUNTRIES = ["PH"]
DEFAULT_PROMO_COUNTRY = "VN"

PROFILE_OVERRIDES = {
    "PH": {
        "browser_locale": "en-PH",
        "browser_timezone": "Asia/Manila",
        "browser_language": "en-PH",
    },
    "SG": {
        "browser_locale": "en-SG",
        "browser_timezone": "Asia/Singapore",
        "browser_language": "en-SG",
    },
    "MY": {
        "browser_locale": "ms-MY",
        "browser_timezone": "Asia/Kuala_Lumpur",
        "browser_language": "ms-MY",
    },
    "ID": {
        "browser_locale": "id-ID",
        "browser_timezone": "Asia/Jakarta",
        "browser_language": "id-ID",
    },
    "TH": {
        "browser_locale": "th-TH",
        "browser_timezone": "Asia/Bangkok",
        "browser_language": "th-TH",
    },
}

SECRETISH = re.compile(
    r"(eyJ[A-Za-z0-9_.-]{40,}|"
    r"pk_live_[A-Za-z0-9]+|"
    r"cuss_secret_[A-Za-z0-9_]+|"
    r"__Secure-next-auth\.session-token=[^;\s]+|"
    r"cf_clearance=[^;\s]+)",
    re.I,
)


def csv_values(value: str, default: list[str]) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return list(default)
    out = [item.strip().upper() for item in re.split(r"[\s,]+", raw) if item.strip()]
    return out or list(default)


def clean_message(value: Any) -> str:
    text = str(value or "")
    text = SECRETISH.sub("[REDACTED]", text)
    text = re.sub(r"(?i)(password|passwd|pwd)=([^&\s]+)", r"\1=[REDACTED]", text)
    return text[:1200]


def short_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 24:
        return text
    return f"{text[:18]}..."


def proxy_label(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlsplit(pay_app.normalize_proxy(value))
        username = unquote(parsed.username or "")
        region = pay_app.proxy_country_hint(value) or pay_app.proxy_country_hint(username)
        sid = ""
        match = re.search(r"(?i)(?:^|[-_])sid[-_=]?([A-Za-z0-9]+)", username)
        if match:
            sid = match.group(1)
        host = parsed.hostname or ""
        host_label = host if not host else re.sub(r"^(.{0,3}).*(.{2})$", r"\1***\2", host)
        parts = [host_label or "proxy"]
        if region:
            parts.append(f"region={region}")
        if sid:
            parts.append("sid=redacted")
        if parsed.username:
            parts.append("auth=redacted")
        return " ".join(parts)
    except Exception:
        hinted = pay_app.proxy_country_hint(value)
        return f"proxy region={hinted or '?'} auth=redacted"


def proxy_for_country(base_proxy: str, country: str) -> str:
    country = str(country or "").strip().upper()
    if not country:
        return pay_app.normalize_proxy(base_proxy)
    return rotate_proxy_session(base_proxy, country)


def stripe_profile(country: str) -> dict[str, str]:
    code = str(country or "US").strip().upper()
    profile = dict(sc._profile(code))
    profile.update(PROFILE_OVERRIDES.get(code, {}))
    return profile


def load_secret(value: str, file_path: str, env_name: str) -> str:
    if value:
        return value
    if file_path:
        return Path(file_path).read_text(encoding="utf-8-sig").strip()
    if env_name:
        return str(os.getenv(env_name) or "").strip()
    return ""


def load_proxies(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    values.extend(args.proxy or [])
    if args.proxy_file:
        values.extend(Path(args.proxy_file).read_text(encoding="utf-8-sig").splitlines())
    if args.proxy_env:
        values.extend(str(os.getenv(args.proxy_env) or "").replace(";", "\n").splitlines())
    proxies: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        normalized = pay_app.normalize_proxy(item)
        if normalized not in seen:
            proxies.append(normalized)
            seen.add(normalized)
    return proxies


def method_snapshot(payload: Any) -> dict[str, Any]:
    methods = pay_app.extract_custom_checkout_methods(payload)
    summary = pay_app.custom_payment_method_summary(payload) if payload else ""
    selected = pay_app.select_custom_checkout_method_from_states("gcash", payload)
    gcash_strings: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if "gcash" in value.lower() and len(gcash_strings) < 20:
                gcash_strings.append(short_id(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return {
        "summary": summary,
        "selected_gcash_method": selected,
        "method_count": len(methods),
        "has_gcash": bool(selected or gcash_strings),
        "gcash_strings": gcash_strings,
    }


def amount_snapshot(payload: Any) -> dict[str, Any]:
    if not payload:
        return {"amount": None, "currency": ""}
    return {
        "amount": pay_app.custom_checkout_amount_minor(payload),
        "currency": pay_app.custom_checkout_currency(payload) or "",
    }


def summarize_custom_state(payload: Any) -> dict[str, Any]:
    summary = method_snapshot(payload)
    summary.update(amount_snapshot(payload))
    return summary


def summarize_stripe_state(init_data: dict[str, Any], elements_data: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    init_types = list(ctx.get("payment_method_types") or [])
    elements_types = [
        str(item.get("type"))
        for item in (elements_data.get("payment_method_specs") or [])
        if isinstance(item, dict) and item.get("type")
    ]
    combined = list(dict.fromkeys(init_types + elements_types))
    gcash_method = next((item for item in combined if "gcash" in item.lower()), "")
    return {
        "summary": ",".join(combined),
        "selected_gcash_method": gcash_method,
        "method_count": len(combined),
        "has_gcash": bool(gcash_method),
        "amount": ctx.get("checkout_amount"),
        "currency": str(ctx.get("currency") or "").upper(),
        "init_payment_method_types": init_types,
        "elements_payment_method_types": elements_types,
    }


def build_promo_http(source_http: Any, promo_proxy: str, meta: dict[str, Any], did: str):
    promo_http = sc.build_http(promo_proxy, impersonate=pay_app.chatgpt_impersonate(meta))
    pay_app.refresh_applied_chatgpt_cookies(promo_http, meta, did)
    try:
        for cookie_name, cookie_value in source_http.cookies.get_dict().items():
            promo_http.cookies.set(cookie_name, cookie_value, domain="chatgpt.com")
        pay_app.refresh_applied_chatgpt_cookies(promo_http, meta, did)
    except Exception:
        pass
    return promo_http


def build_checkout_payload(
    checkout_country: str,
    ui_mode: str,
    apply_promo_on_create: bool,
    campaign: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkout_country = str(checkout_country or "PH").strip().upper()
    currency, _source = pay_app.normalize_checkout_currency(checkout_country, "")
    options = {
        "plan": "plus",
        "link_type": "gcash",
        "country": checkout_country,
        "currency": currency,
        "checkout_country": checkout_country,
        "checkout_currency": currency,
        "use_promo": True,
        "promo_campaign": campaign,
        "promo_on_create": bool(apply_promo_on_create),
    }
    payload = pay_app.checkout_payload(dict(options), {})
    payload["checkout_ui_mode"] = str(ui_mode or "redirect").strip().lower()
    return payload, options


def probe_one(
    token: str,
    meta: dict[str, Any],
    base_proxy: str,
    checkout_country: str,
    proxy_country: str,
    ui_mode: str,
    profile_country: str,
    promo_country: str,
    campaign: str,
    *,
    apply_promo: bool,
    submit_taxes: bool,
    promo_on_create: bool,
) -> dict[str, Any]:
    checkout_proxy = proxy_for_country(base_proxy, proxy_country)
    promo_proxy = proxy_for_country(base_proxy, promo_country)
    device_id = str(uuid.uuid4())
    did = pay_app.chatgpt_cookie_did(meta) or device_id
    payload, options = build_checkout_payload(checkout_country, ui_mode, promo_on_create, campaign)
    row: dict[str, Any] = {
        "checkout_country": checkout_country,
        "checkout_currency": options["checkout_currency"],
        "proxy_country": proxy_country,
        "promo_country": promo_country if apply_promo else "",
        "ui_mode": ui_mode,
        "profile_country": profile_country,
        "proxy": proxy_label(checkout_proxy),
        "promo_proxy": proxy_label(promo_proxy) if apply_promo else "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": False,
        "error": "",
    }
    logs: list[str] = []

    created = pay_app.create_checkout(
        token,
        payload,
        checkout_proxy,
        device_id,
        did,
        lambda message: logs.append(clean_message(message)),
        use_sen=True,
        use_so=True,
        credential_meta=meta,
    )
    checkout_data = created["data"]
    chatgpt_http = created["http"]
    session_id = str(checkout_data.get("checkout_session_id") or "")
    processor = str(checkout_data.get("processor_entity") or "").strip() or (
        "openai_llc" if checkout_country == "US" else "openai_ie"
    )
    row.update(
        {
            "session_id": short_id(session_id),
            "session_kind": "oaics" if session_id.startswith("oaics_") else ("cs" if session_id.startswith("cs_") else ""),
            "checkout_provider": str(checkout_data.get("checkout_provider") or ""),
            "processor_entity": processor,
            "has_publishable_key": bool(checkout_data.get("publishable_key")),
        }
    )

    states: list[Any] = [checkout_data]
    if session_id.startswith("oaics_"):
        custom_state = pay_app.fetch_custom_checkout_session(
            chatgpt_http, token, session_id, processor, device_id
        )
        states.append(custom_state)
        row["initial"] = summarize_custom_state({"states": states})
        currency = pay_app.custom_checkout_currency(custom_state) or options["checkout_currency"]
        if apply_promo:
            promo_http = build_promo_http(chatgpt_http, promo_proxy, meta, did)
            update_state = pay_app.update_checkout_promo(
                promo_http,
                token,
                session_id,
                processor,
                campaign,
                lambda message: logs.append(clean_message(message)),
                device_id=device_id,
            )
            states.append(update_state)
            refreshed = pay_app.fetch_custom_checkout_session(
                chatgpt_http, token, session_id, processor, device_id
            )
            states.append(refreshed)
            row["after_promo"] = summarize_custom_state({"states": states})
            currency = pay_app.custom_checkout_currency(refreshed) or currency
        if submit_taxes:
            billing = default_billing(checkout_country, str(meta.get("email") or ""), real_random=False)
            tax_state = pay_app.submit_custom_checkout_taxes(
                chatgpt_http,
                token,
                session_id,
                processor,
                billing,
                currency or "PHP",
                device_id,
            )
            states.append(tax_state)
            row["after_taxes"] = summarize_custom_state({"states": states})
        final = summarize_custom_state({"states": states})
        row["final"] = final
        row["ok"] = bool(final.get("has_gcash"))
        row["selected_gcash_method"] = final.get("selected_gcash_method") or ""
        row["method_summary"] = final.get("summary") or ""
        row["amount"] = final.get("amount")
        row["amount_currency"] = final.get("currency") or ""
    elif session_id.startswith("cs_"):
        stripe_http = sc.build_http(checkout_proxy)
        pk = str(checkout_data.get("publishable_key") or "") or sc.verify_pk(
            stripe_http, session_id, lambda message: logs.append(clean_message(message))
        )
        profile = stripe_profile(profile_country or checkout_country)
        init_data, version, ctx = sc.init_checkout(
            stripe_http, session_id, pk, profile, lambda message: logs.append(clean_message(message))
        )
        if apply_promo:
            promo_http = build_promo_http(chatgpt_http, promo_proxy, meta, did)
            pay_app.update_checkout_promo(
                promo_http,
                token,
                session_id,
                processor,
                campaign,
                lambda message: logs.append(clean_message(message)),
                device_id=device_id,
            )
            init_data, version, ctx = sc.init_checkout(
                stripe_http, session_id, pk, profile, lambda message: logs.append(clean_message(message))
            )
        elements_data = sc.fetch_elements_session(
            stripe_http,
            pk,
            session_id,
            ctx,
            version,
            profile,
            lambda message: logs.append(clean_message(message)),
        )
        final = summarize_stripe_state(init_data, elements_data, ctx)
        row["final"] = final
        row["ok"] = bool(final.get("has_gcash"))
        row["selected_gcash_method"] = final.get("selected_gcash_method") or ""
        row["method_summary"] = final.get("summary") or ""
        row["amount"] = final.get("amount")
        row["amount_currency"] = final.get("currency") or ""
    else:
        row["final"] = summarize_custom_state(checkout_data)
        row["method_summary"] = row["final"].get("summary") or ""

    row["logs"] = logs[-12:]
    return row


def print_row(row: dict[str, Any]) -> None:
    status = "FOUND" if row.get("ok") else "MISS"
    head = (
        f"{status} checkout={row.get('checkout_country')}/{row.get('checkout_currency')} "
        f"proxy={row.get('proxy_country')} promo={row.get('promo_country') or '-'} "
        f"ui={row.get('ui_mode')} profile={row.get('profile_country')} "
        f"sid={row.get('session_id') or '-'} kind={row.get('session_kind') or '-'}"
    )
    print(head, flush=True)
    print(
        "  methods={} selected={} amount={} {}".format(
            row.get("method_summary") or "-",
            row.get("selected_gcash_method") or "-",
            "-" if row.get("amount") is None else row.get("amount"),
            row.get("amount_currency") or "",
        ).rstrip(),
        flush=True,
    )
    if row.get("error"):
        print(f"  error={row['error']}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe GCash availability across checkout/proxy country combinations."
    )
    parser.add_argument("--token", default="", help="Access Token or Session JSON. Prefer --token-file/env for shell history safety.")
    parser.add_argument("--token-file", default="", help="File containing Access Token or Session JSON.")
    parser.add_argument("--token-env", default="PAY153_TOKEN_RAW", help="Environment variable holding token/session JSON.")
    parser.add_argument("--proxy", action="append", default=[], help="Base proxy. Repeatable.")
    parser.add_argument("--proxy-file", default="", help="File with one base proxy per line.")
    parser.add_argument("--proxy-env", default="PAY153_GCASH_PROXIES", help="Environment variable with newline/semicolon separated proxies.")
    parser.add_argument("--checkout-countries", default=",".join(DEFAULT_CHECKOUT_COUNTRIES))
    parser.add_argument("--proxy-countries", default=",".join(DEFAULT_PROXY_COUNTRIES))
    parser.add_argument("--profile-countries", default=",".join(DEFAULT_PROFILE_COUNTRIES))
    parser.add_argument(
        "--paired-countries",
        default="",
        help="Run only paired checkout/proxy/profile countries, for example PH,SG,MY.",
    )
    parser.add_argument("--ui-modes", default=",".join(DEFAULT_UI_MODES))
    parser.add_argument("--promo-country", default=DEFAULT_PROMO_COUNTRY)
    parser.add_argument("--campaign", default="plus-1-month-free")
    parser.add_argument("--no-promo", action="store_true", help="Skip checkout/update promo probe.")
    parser.add_argument("--promo-on-create", action="store_true", help="Attach promo_campaign on checkout create.")
    parser.add_argument("--no-taxes", action="store_true", help="Skip PH tax/address submit for OAICS sessions.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum matrix rows to run. 0 means all.")
    parser.add_argument("--jsonl", default="", help="Optional JSONL output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_token = load_secret(args.token, args.token_file, args.token_env)
    if not raw_token:
        raise SystemExit("missing token: pass --token-file or set PAY153_TOKEN_RAW")
    token, meta = pay_app.extract_access_token(raw_token)
    proxies = load_proxies(args)
    if not proxies:
        raise SystemExit("missing proxy: pass --proxy-file/--proxy or set PAY153_GCASH_PROXIES")

    ui_modes = [item.lower() for item in csv_values(args.ui_modes, DEFAULT_UI_MODES)]
    promo_country = str(args.promo_country or DEFAULT_PROMO_COUNTRY).strip().upper()
    rows: list[tuple[str, str, str, str, str]] = []
    paired_countries = csv_values(args.paired_countries, []) if str(args.paired_countries or "").strip() else []
    if paired_countries:
        for proxy_index, _proxy in enumerate(proxies):
            for country in paired_countries:
                for ui_mode in ui_modes:
                    rows.append((str(proxy_index), country, country, ui_mode, country))
    else:
        checkout_countries = csv_values(args.checkout_countries, DEFAULT_CHECKOUT_COUNTRIES)
        proxy_countries = csv_values(args.proxy_countries, DEFAULT_PROXY_COUNTRIES)
        profile_countries = csv_values(args.profile_countries, DEFAULT_PROFILE_COUNTRIES)
        for proxy_index, _proxy in enumerate(proxies):
            for checkout_country in checkout_countries:
                for proxy_country in proxy_countries:
                    for ui_mode in ui_modes:
                        for profile_country in profile_countries:
                            rows.append((str(proxy_index), checkout_country, proxy_country, ui_mode, profile_country))
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    jsonl_path = Path(args.jsonl) if args.jsonl else None
    if jsonl_path:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    found = False
    print(f"matrix_rows={len(rows)} apply_promo={not args.no_promo} submit_taxes={not args.no_taxes}", flush=True)
    for ordinal, (proxy_index_text, checkout_country, proxy_country, ui_mode, profile_country) in enumerate(rows, 1):
        proxy_index = int(proxy_index_text)
        row: dict[str, Any]
        try:
            print(f"[{ordinal}/{len(rows)}] probing checkout={checkout_country} proxy={proxy_country} ui={ui_mode} profile={profile_country}", flush=True)
            row = probe_one(
                token,
                meta,
                proxies[proxy_index],
                checkout_country,
                proxy_country,
                ui_mode,
                profile_country,
                promo_country,
                args.campaign,
                apply_promo=not args.no_promo,
                submit_taxes=not args.no_taxes,
                promo_on_create=bool(args.promo_on_create),
            )
        except Exception as exc:
            checkout_currency, _source = pay_app.normalize_checkout_currency(checkout_country, "")
            row = {
                "checkout_country": checkout_country,
                "checkout_currency": checkout_currency,
                "proxy_country": proxy_country,
                "promo_country": promo_country if not args.no_promo else "",
                "ui_mode": ui_mode,
                "profile_country": profile_country,
                "proxy": proxy_label(proxy_for_country(proxies[proxy_index], proxy_country)),
                "ok": False,
                "error": clean_message(f"{type(exc).__name__}: {exc}"),
            }
        found = found or bool(row.get("ok"))
        print_row(row)
        if jsonl_path:
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
