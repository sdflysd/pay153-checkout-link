#!/usr/bin/env node
"use strict";

const http = require("http");
const WebSocket = require("ws");

const args = process.argv.slice(2);
const opts = {
  cdpUrl: "http://127.0.0.1:9222",
  processor: "openai_ie",
  sessionId: "",
  waitMs: 5000,
};

for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--cdp" && args[i + 1]) opts.cdpUrl = args[++i];
  else if (arg === "--processor" && args[i + 1]) opts.processor = args[++i];
  else if (arg === "--session" && args[i + 1]) opts.sessionId = args[++i];
  else if (arg === "--wait-ms" && args[i + 1]) opts.waitMs = Number(args[++i]) || opts.waitMs;
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: 5000 }, (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
  });
}

function connectCdp(wsUrl, timeoutMs = 20000) {
  const ws = new WebSocket(wsUrl);
  let nextId = 0;
  const pending = new Map();
  const opened = new Promise((resolve, reject) => {
    ws.once("open", resolve);
    ws.once("error", reject);
  });
  ws.on("message", (raw) => {
    const message = JSON.parse(String(raw));
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result || {});
  });
  return {
    async send(method, params = {}) {
      await opened;
      const id = ++nextId;
      ws.send(JSON.stringify({ id, method, params }));
      return await new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        setTimeout(() => {
          if (pending.delete(id)) reject(new Error(`${method} timeout`));
        }, timeoutMs);
      });
    },
    close() {
      try {
        ws.close();
      } catch (_error) {
        // Best effort.
      }
    },
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanValue(value) {
  if (typeof value === "string") {
    if (/^(pk|sk|rk|sess|ctoken|cs)_/i.test(value)) return `${value.slice(0, 18)}...`;
    if (value.length > 120) return `${value.slice(0, 120)}...`;
    return value;
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const key of [
      "id", "type", "name", "display_name", "label", "value", "code",
      "payment_method_type", "paymentMethodType", "custom_payment_method_type_id",
      "customPaymentMethodTypeId", "external_payment_method_type",
      "externalPaymentMethodType",
    ]) {
      if (value[key] != null) out[key] = cleanValue(value[key]);
    }
    return Object.keys(out).length ? out : Object.keys(value).slice(0, 20);
  }
  return value;
}

function methodPaths(value, path = "", out = []) {
  const methodKey = /(?:custom|external|available)?_?payment_?method|payment_method_types|paymentMethodTypes|customPaymentMethods|externalPaymentMethods|availablePaymentMethods|payment_method_specs/i;
  if (Array.isArray(value)) {
    const leaf = path.split(".").pop() || "";
    if (methodKey.test(leaf)) {
      out.push({ path, value: value.slice(0, 40).map(cleanValue) });
    }
    value.forEach((item, index) => methodPaths(item, `${path}[${index}]`, out));
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      methodPaths(item, path ? `${path}.${key}` : key, out);
    }
  }
  return out;
}

function findStrings(value, predicate, path = "", out = []) {
  if (typeof value === "string") {
    if (predicate(value)) out.push({ path, value: cleanValue(value) });
  } else if (Array.isArray(value)) {
    value.forEach((item, index) => findStrings(item, predicate, `${path}[${index}]`, out));
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      findStrings(item, predicate, path ? `${path}.${key}` : key, out);
    }
  }
  return out;
}

function summarizeCheckout(payload) {
  const state = payload.checkout_state || payload.checkoutState || {};
  return {
    checkout_session_id: cleanValue(payload.checkout_session_id || payload.id || ""),
    processor_entity: payload.processor_entity || "",
    checkout_ui_mode: payload.checkout_ui_mode || "",
    checkout_provider: payload.checkout_provider || "",
    status: payload.status || "",
    payment_status: payload.payment_status || "",
    billing_details: payload.billing_details || {},
    currency: payload.currency || state.currency || "",
    checkout_state_id: cleanValue(state.id || ""),
    checkout_state_canConfirm: state.canConfirm,
    checkout_state_total: state.total || "",
    payment_method_types: payload.payment_method_types || [],
    custom_payment_methods_count: Array.isArray(payload.custom_payment_methods)
      ? payload.custom_payment_methods.length
      : null,
    method_paths: methodPaths(payload).slice(0, 120),
    gcash_strings: findStrings(payload, (text) => /gcash|external_gcash/i.test(text)).slice(0, 80),
    cs_strings: findStrings(payload, (text) => /^cs_(?:live|test)_/i.test(text)).slice(0, 20),
    has_publishable_key: Boolean(payload.publishable_key),
    publishable_key_prefix: payload.publishable_key ? `${String(payload.publishable_key).slice(0, 18)}...` : "",
  };
}

async function stripeInitInPage(client, csId, publishableKey) {
  if (!/^cs_(live|test)_[A-Za-z0-9]+$/.test(String(csId || "")) || !publishableKey) {
    return null;
  }
  const expression = `(
    async (input) => {
      const params = new URLSearchParams();
      params.set("browser_locale", "en-PH");
      params.set("browser_timezone", "Asia/Manila");
      params.set("elements_session_client[elements_init_source]", "custom_checkout");
      params.set("elements_session_client[referrer_host]", "chatgpt.com");
      params.set("elements_session_client[stripe_js_id]", crypto.randomUUID());
      params.set("elements_session_client[locale]", "en-PH");
      params.set("elements_session_client[is_aggregation_expected]", "false");
      params.set("key", input.publishableKey);
      params.set("_stripe_version", "2025-03-31.basil");
      const response = await fetch("https://api.stripe.com/v1/payment_pages/" + input.csId + "/init", {
        method: "POST",
        headers: {"content-type": "application/x-www-form-urlencoded"},
        body: params.toString()
      });
      const text = await response.text();
      let json = null;
      try { json = JSON.parse(text); } catch (_error) {}
      return {status: response.status, ok: response.ok, text: text.slice(0, 300), json};
    }
  )(${JSON.stringify({ csId, publishableKey })})`;
  const result = await client.send("Runtime.evaluate", {
    awaitPromise: true,
    returnByValue: true,
    expression,
  });
  const value = (result.result && result.result.value) || {};
  const payload = value.json || {};
  return {
    status: value.status || 0,
    method_paths: methodPaths(payload).slice(0, 120),
    gcash_strings: findStrings(payload, (text) => /gcash|external_gcash/i.test(text)).slice(0, 80),
    payment_method_types: payload.payment_method_types || [],
    payment_method_specs: Array.isArray(payload.payment_method_specs)
      ? payload.payment_method_specs.map((item) => cleanValue(item)).slice(0, 40)
      : [],
    raw_head: value.text || "",
  };
}

async function main() {
  const cdpBase = String(opts.cdpUrl || "").replace(/\/+$/, "");
  const tabs = await getJson(`${cdpBase}/json/list`);
  const pages = Array.isArray(tabs) ? tabs.filter((tab) => tab.type === "page") : [];
  let page = pages.find((tab) => String(tab.url || "").startsWith("https://chatgpt.com/"));
  page = page || pages[0];
  if (!page || !page.webSocketDebuggerUrl) throw new Error("no debuggable page");
  const client = connectCdp(page.webSocketDebuggerUrl);
  try {
    await client.send("Page.enable").catch(() => ({}));
    if (opts.sessionId) {
      await client.send("Page.navigate", {
        url: `https://chatgpt.com/checkout/${opts.processor}/${opts.sessionId}`,
      });
      await sleep(opts.waitMs);
    }
    const evaluated = await client.send("Runtime.evaluate", {
      awaitPromise: true,
      returnByValue: true,
      expression: `(
        async (input) => {
          const auth = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" });
          const authJson = await auth.json().catch(() => ({}));
          const token = authJson.accessToken || authJson.access_token || "";
          const out = {
            href: location.href,
            title: document.title,
            bodyText: (document.body && document.body.innerText || "").slice(0, 1200),
            authStatus: auth.status,
            hasToken: Boolean(token),
            userEmailPresent: Boolean(authJson.user && authJson.user.email),
            accountPresent: Boolean(authJson.account && authJson.account.id),
            frames: [...document.querySelectorAll("iframe")].map((frame) => {
              const rect = frame.getBoundingClientRect();
              return { title: frame.title || "", src: frame.src || "", width: rect.width, height: rect.height };
            }),
            resources: performance.getEntriesByType("resource")
              .map((entry) => entry.name)
              .filter((url) => /stripe|checkout|payment|gcash/i.test(url))
              .slice(-80)
          };
          if (input.sessionId && token) {
            const checkout = await fetch("/backend-api/payments/checkout/" + input.processor + "/" + input.sessionId, {
              credentials: "include",
              cache: "no-store",
              headers: { Authorization: "Bearer " + token, Accept: "application/json" }
            });
            out.checkoutStatus = checkout.status;
            const text = await checkout.text();
            out.checkoutRawHead = text.slice(0, 300);
            try { out.checkoutJson = JSON.parse(text); } catch (_error) { out.checkoutJson = null; }
          }
          return out;
        }
      )(${JSON.stringify({ processor: opts.processor, sessionId: opts.sessionId })})`,
    });
    const pageState = (evaluated.result && evaluated.result.value) || {};
    const checkoutJson = pageState.checkoutJson || {};
    const summary = {
      ok: Boolean(pageState.hasToken),
      cdpUrl: cdpBase,
      href: pageState.href || "",
      title: pageState.title || "",
      authStatus: pageState.authStatus || 0,
      hasToken: Boolean(pageState.hasToken),
      userEmailPresent: Boolean(pageState.userEmailPresent),
      accountPresent: Boolean(pageState.accountPresent),
      bodyHasGcash: /gcash/i.test(String(pageState.bodyText || "")),
      bodyTextHead: pageState.bodyText || "",
      frames: pageState.frames || [],
      resources: pageState.resources || [],
      checkoutStatus: pageState.checkoutStatus || 0,
      checkoutRawHead: pageState.checkoutRawHead || "",
      checkout: checkoutJson && typeof checkoutJson === "object" ? summarizeCheckout(checkoutJson) : null,
      stripeInit: null,
    };
    const checkoutState = checkoutJson.checkout_state || checkoutJson.checkoutState || {};
    const nestedCs = String(checkoutState.id || "");
    const topCs = String(checkoutJson.checkout_session_id || "");
    const csId = /^cs_(?:live|test)_/.test(nestedCs) ? nestedCs : (/^cs_(?:live|test)_/.test(topCs) ? topCs : "");
    if (csId && checkoutJson.publishable_key) {
      summary.stripeInit = await stripeInitInPage(client, csId, checkoutJson.publishable_key);
    }
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
  } finally {
    client.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
