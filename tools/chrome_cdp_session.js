#!/usr/bin/env node
"use strict";

const http = require("http");
const WebSocket = require("ws");

const cdpBase = String(process.argv[2] || "http://127.0.0.1:9222").replace(/\/+$/, "");
const paymentCookieNames = new Set([
  "oai-did",
  "oai-hlib",
  "oai-sc",
  "oaicom-stable-id",
  "_account",
  "_account_is_fedramp",
  "__Host-next-auth.csrf-token",
  "__Secure-next-auth.callback-url",
  "__Secure-next-auth.csrf-token",
  "__Secure-oai-is",
  "__Secure-next-auth.session-token",
  "__cf_bm",
  "__cflb",
  "_cfuvid",
  "__oailb",
  "cf_clearance",
]);

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
        }, 8000);
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

function isChatgptCookie(cookie) {
  const domain = String(cookie.domain || "");
  return (domain === "chatgpt.com" || domain === ".chatgpt.com") && paymentCookieNames.has(cookie.name);
}

async function main() {
  const tabs = await getJson(`${cdpBase}/json/list`);
  const page = (Array.isArray(tabs) ? tabs : []).find((tab) => String(tab.url || "").startsWith("https://chatgpt.com/"));
  if (!page || !page.webSocketDebuggerUrl) {
    throw new Error("no chatgpt.com page on Chrome CDP");
  }
  const client = connectCdp(page.webSocketDebuggerUrl);
  try {
    await client.send("Network.enable");
    const allCookies = await client.send("Network.getAllCookies");
    const cookies = {};
    for (const cookie of allCookies.cookies || []) {
      if (isChatgptCookie(cookie) && cookie.value) {
        cookies[cookie.name] = cookie.value;
      }
    }
    const evaluated = await client.send("Runtime.evaluate", {
      awaitPromise: true,
      returnByValue: true,
      expression: `(
        async () => {
          const response = await fetch("https://chatgpt.com/api/auth/session", { credentials: "include" });
          const text = await response.text();
          let payload = {};
          try { payload = JSON.parse(text); } catch (_error) {}
          return {
            status: response.status,
            accessToken: payload.accessToken || payload.access_token || "",
            account: payload.account || {},
            user: payload.user || {},
            userAgent: navigator.userAgent || "",
            language: navigator.language || "",
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || ""
          };
        }
      )()`,
    });
    const session = (evaluated.result && evaluated.result.value) || {};
    const output = {
      ok: Boolean(session.accessToken && cookies["__Secure-next-auth.session-token"]),
      source: "chrome_cdp",
      pageUrl: page.url || "",
      pageTitle: page.title || "",
      status: session.status || 0,
      accessToken: session.accessToken || "",
      account: session.account || {},
      user: session.user || {},
      userAgent: session.userAgent || "",
      language: session.language || "",
      timeZone: session.timeZone || "",
      cookies,
    };
    if (!output.ok) {
      output.error = `missing ${session.accessToken ? "" : "accessToken"} ${cookies["__Secure-next-auth.session-token"] ? "" : "sessionCookie"}`.trim();
    }
    process.stdout.write(`${JSON.stringify(output)}\n`);
  } finally {
    client.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
