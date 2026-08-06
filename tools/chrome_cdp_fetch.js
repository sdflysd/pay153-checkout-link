#!/usr/bin/env node
"use strict";

const fs = require("fs");
const http = require("http");
const WebSocket = require("ws");

const inputPath = process.argv[2];
if (!inputPath) {
  process.stderr.write("missing input path or '-'\n");
  process.exit(2);
}
const inputRaw = inputPath === "-" ? fs.readFileSync(0, "utf-8") : fs.readFileSync(inputPath, "utf-8");
const input = JSON.parse(inputRaw);
const cdpBase = String(input.cdpUrl || "http://127.0.0.1:9222").replace(/\/+$/, "");

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

function connectCdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 0;
  const pending = new Map();
  const opened = new Promise((resolve, reject) => {
    ws.once("open", resolve);
    ws.once("error", reject);
  });
  ws.on("message", (raw) => {
    const message = JSON.parse(String(raw));
    if (!message.id || !pending.has(message.id)) {
      return;
    }
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) {
      reject(new Error(JSON.stringify(message.error)));
    } else {
      resolve(message.result || {});
    }
  });
  return {
    async send(method, params = {}) {
      await opened;
      const id = ++nextId;
      ws.send(JSON.stringify({ id, method, params }));
      return await new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        setTimeout(() => {
          if (pending.delete(id)) {
            reject(new Error(`${method} timeout`));
          }
        }, Number(input.timeoutMs || 20000));
      });
    },
    close() {
      try {
        ws.close();
      } catch (_error) {
        // Best effort cleanup.
      }
    },
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function clickAt(client, x, y) {
  await client.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await client.send("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
  await client.send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
}

async function selectPaymentMethod(client, method) {
  const normalized = String(method || "").toLowerCase();
  if (!normalized || normalized.startsWith("cpmt_")) {
    return null;
  }
  const result = await client.send("Runtime.evaluate", {
    returnByValue: true,
    expression: `(() => {
      const frames = [...document.querySelectorAll("iframe")].map((frame) => {
        const rect = frame.getBoundingClientRect();
        return {
          title: frame.title || "",
          src: frame.src || "",
          x: rect.x,
          y: rect.y,
          w: rect.width,
          h: rect.height
        };
      });
      return frames.find((frame) =>
        frame.w > 200 &&
        frame.h > 120 &&
        (frame.title.includes("支付") || frame.src.includes("componentName=payment"))
      ) || null;
    })()`,
  });
  const frame = result.result && result.result.value;
  if (!frame || !frame.w || !frame.h) {
    return { selected: false, reason: "payment iframe not found" };
  }
  let offsetX = 0;
  if (normalized === "card") {
    offsetX = frame.w * 0.125;
  } else if (normalized === "kr_card") {
    offsetX = frame.w * 0.375;
  } else if (normalized === "kakao_pay") {
    offsetX = frame.w * 0.625;
  } else if (normalized === "naver_pay") {
    offsetX = frame.w * 0.875;
  } else {
    return { selected: false, reason: `unsupported method ${normalized}` };
  }
  const x = Math.round(frame.x + offsetX);
  const y = Math.round(frame.y + Math.min(44, Math.max(34, frame.h * 0.15)));
  await clickAt(client, x, y);
  await sleep(Number(input.selectSettleMs || 900));
  return { selected: true, method: normalized, x, y };
}

function sanitizeHeaders(headers) {
  const blocked = new Set([
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "host",
    "origin",
    "referer",
    "user-agent",
  ]);
  const out = {};
  for (const [key, value] of Object.entries(headers || {})) {
    const name = String(key || "").toLowerCase();
    if (!name || blocked.has(name) || name.startsWith("sec-") || name.startsWith("proxy-")) {
      continue;
    }
    out[key] = String(value);
  }
  return out;
}

async function main() {
  const tabs = await getJson(`${cdpBase}/json/list`);
  const targetPageUrl = String(input.pageUrl || input.referrer || "").trim();
  const pages = Array.isArray(tabs) ? tabs : [];
  const page = (
    (targetPageUrl && pages.find((tab) => String(tab.url || "").startsWith(targetPageUrl)))
    || pages.find((tab) => String(tab.url || "").startsWith("https://chatgpt.com/checkout/"))
    || pages.find((tab) => String(tab.url || "").startsWith("https://chatgpt.com/"))
  );
  if (!page || !page.webSocketDebuggerUrl) {
    throw new Error("no chatgpt.com page on Chrome CDP");
  }
  const client = connectCdp(page.webSocketDebuggerUrl);
  try {
    const pageUrl = String(input.pageUrl || "").trim();
    if (pageUrl.startsWith("https://chatgpt.com/")) {
      await client.send("Page.enable").catch(() => ({}));
      const current = await client.send("Runtime.evaluate", {
        returnByValue: true,
        expression: "location.href",
      }).catch(() => ({}));
      const currentHref = String(current.result && current.result.value || "");
      if (!currentHref.startsWith(pageUrl)) {
        await client.send("Page.navigate", { url: pageUrl });
        const deadline = Date.now() + Number(input.pageReadyMs || 12000);
        while (Date.now() < deadline) {
          const state = await client.send("Runtime.evaluate", {
            returnByValue: true,
            expression: "({ href: location.href, ready: document.readyState })",
          }).catch(() => ({}));
          const value = (state.result && state.result.value) || {};
          if (String(value.href || "").startsWith(pageUrl) && ["interactive", "complete"].includes(String(value.ready || ""))) {
            break;
          }
          await sleep(250);
        }
        await sleep(Number(input.settleMs || 1800));
      }
    }
    const payload = {
      url: input.url,
      method: input.method || "POST",
      body: input.body || {},
      headers: sanitizeHeaders(input.headers || {}),
      referrer: input.referrer || "https://chatgpt.com/",
    };
    let selection = null;
    const selectMethod = String(input.selectPaymentMethod || "").trim();
    if (selectMethod) {
      selection = await selectPaymentMethod(client, selectMethod).catch((error) => ({
        selected: false,
        reason: String(error && error.message ? error.message : error),
      }));
    }
    const expression = `(
      async (input) => {
        try {
          const response = await fetch(input.url, {
            method: input.method,
            credentials: "include",
            cache: "no-store",
            referrer: input.referrer,
            headers: input.headers,
            body: JSON.stringify(input.body)
          });
          const text = await response.text();
          let json = null;
          try { json = JSON.parse(text); } catch (_error) {}
          return {
            status: response.status,
            ok: response.ok,
            url: response.url,
            text,
            json,
            pageHref: location.href,
            selection: input.selection || null,
            userAgent: navigator.userAgent || ""
          };
        } catch (error) {
          return {
            status: 0,
            ok: false,
            text: "",
            json: null,
            error: String(error && error.message ? error.message : error),
            pageHref: location.href,
            selection: input.selection || null,
            userAgent: navigator.userAgent || ""
          };
        }
      }
    )(${JSON.stringify({ ...payload, selection })})`;
    const result = await client.send("Runtime.evaluate", {
      awaitPromise: true,
      returnByValue: true,
      expression,
    });
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 500));
    }
    process.stdout.write(`${JSON.stringify((result.result && result.result.value) || {})}\n`);
  } finally {
    client.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
