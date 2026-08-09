# GCash 核心逻辑与 AI 提示词部署说明

本文档把当前仓库里的 GCash 提链逻辑整理成一份可独立给服务器端 AI 使用的 Markdown 规范。它的目标不是复述代码，而是让 AI 在没有完整上下文时，也能按稳定规则判断、执行、诊断 GCash 流程，并能按相同链路重写一个 Web 应用。

如果目标是“让另一台电脑上的 AI 重新实现”，必须把本文当作实现契约使用，而不是只当说明书使用：第 2-18 节定义 GCash 支付链路，第 19-24 节定义 Web 应用、服务端模块、接口和验收标准。

> 安全要求：部署到服务器的 AI 提示词和日志都不得输出真实 Access Token、Session JSON、Cookie、代理密码、`pk_live_*`、`cuss_secret_*`、`cf_clearance` 等敏感值。日志里只允许保留脱敏后的地区、状态、方法名、金额和错误摘要。

## 1. 代码来源

核心逻辑来自以下文件：

- `app.py`
  - `/api/checkout` 入参归一化与任务创建
  - 代理池选择、重试包装、GCash 主流程
  - OAICS 自定义 Checkout 的读取、优惠更新、PH 账单提交、支付方式确认和启动
  - GCash 支付方式识别、预选页面降级、金额校验
- `provider_checkout.py`
  - provider 默认国家/币种
  - 标准 Stripe `cs_*` Checkout 分支
  - `gcash -> external_gcash` 映射
  - PH 默认账单地址
- `tools/gcash_country_probe.py`
  - GCash 可用性探测矩阵
  - 只读探测：创建 Checkout、可选优惠更新、可选 taxes，不 confirm、不 start
- `tests/test_checkout_routing.py`
  - GCash 代理默认值、Checkout UI mode、支付方式选择、降级开关、start payload 的回归测试
- `tests/test_provider_mapping.py`
  - `external_gcash` 映射和非 0 优惠金额返回行为

## 2. 不可变核心规则

服务器 AI 处理 GCash 时必须遵守这些规则：

1. GCash 的默认账单国家和币种固定为 `PH/PHP`。
2. 创建 OpenAI Checkout 时，request country 必须与 billing country 一致。不要再用 `US` 创建 `PH/PHP` 账单，否则会上游报错：`Billing country must match request country.`
3. 代理池 1 用于 GCash Checkout 创建、读取支付方式、提交 PH 账单、确认和启动支付。
4. 代理池 2 只用于优惠预检和 `checkout/update` 优惠更新，默认地区是 `promo_country`，如果未传则默认为 `VN`。
5. GCash 创建 Checkout 时使用 `checkout_ui_mode = "redirect"`，不要在创建阶段默认携带 `promo_campaign`。
6. GCash 的 Stripe canonical payment method 是 `external_gcash`，但官方响应里也可能出现原生 `gcash` 或匿名 `cpmt_*`。
7. `external_gcash` 不能走 Stripe `confirmation_token` 流程，应走 OpenAI custom/external start 路径。
8. 原生 `gcash` 才需要先创建 Stripe `confirmation_token`，再提交 OpenAI `checkout/confirm`。
9. 只有 `card,link` 时不能认为 GCash 可用。
10. `amount = 0` 才表示优惠金额已确认归零；`amount = None` 是金额待定；非 0 金额表示优惠未命中，但不一定阻止返回 Checkout 链。
11. 当后端没有列出 GCash 且 `external_gcash` direct start 也失败时，只有显式允许页面降级，才返回预选 Checkout 页面；默认必须报错。

## 3. 输入字段规范

推荐给 AI 的最小业务输入：

```json
{
  "plan": "plus",
  "link_type": "gcash",
  "country": "PH",
  "currency": "PHP",
  "checkout_country": "PH",
  "checkout_currency": "PHP",
  "use_promo": true,
  "promo_country": "VN",
  "promo_campaign": "plus-1-month-free",
  "entry_proxies": ["PH checkout proxy"],
  "exit_proxies": ["promo proxy"],
  "retry_count": 10,
  "allow_gcash_page_fallback": false
}
```

说明：

- `token_raw` 可以是 Access Token，也可以是 Session JSON；解析后必须得到 Bearer token 和尽可能完整的 ChatGPT Cookie 元数据。
- `entry_proxies` 必填，建议 PH。
- `exit_proxies` 必填，因为 GCash 需要独立优惠更新线路。
- `promo_country` 不传时，系统默认把代理池 2 目标国家设为 `VN`。
- `allow_gcash_page_fallback` 默认关闭；也可以用环境变量 `PAY153_ALLOW_GCASH_PAGE_FALLBACK=1` 打开。

## 4. 代理路由

GCash 当前不是双向支付代理，而是“PH Checkout + 独立优惠更新”路由：

| 阶段 | 使用代理 | 默认国家 | 说明 |
|---|---|---:|---|
| 创建 Checkout | 代理池 1 | PH | country/billing 必须是 PH/PHP |
| 读取 OAICS Checkout | 代理池 1 | PH | 读取支付方式、金额、publishable key |
| 优惠预检 | 代理池 2 | VN 或用户指定 | `preflight_trial_eligibility` |
| `checkout/update` | 代理池 2 | VN 或用户指定 | 应用 `plus-1-month-free` |
| PH taxes | 代理池 1 | PH | 提交 PH 账单地址 |
| confirm/start | 代理池 1 | PH | GCash 确认和外部跳转启动 |
| 标准 Stripe `cs_*` 初始化 | 代理池 1 | PH | `stripe_to_provider()` 中 GCash 特例 |

默认国家函数行为：

```text
default_entry_proxy_country("gcash", "PH") -> "PH"
default_exit_proxy_country("gcash", "PH", promo_country="", use_promo=True) -> "VN"
default_exit_proxy_country("gcash", "PH", promo_country="PH", use_promo=True) -> "PH"
```

## 5. Checkout 创建载荷

GCash 的 OpenAI Checkout 创建载荷应包含：

```json
{
  "entry_point": "all_plans_pricing_modal",
  "plan_name": "chatgptplusplan",
  "billing_details": {
    "country": "PH",
    "currency": "PHP"
  },
  "cancel_url": "https://chatgpt.com/",
  "checkout_ui_mode": "redirect",
  "check_card_proxy": true
}
```

注意：

- `use_promo=true` 时，GCash 默认不在 create 阶段附加 `promo_campaign`。
- 优惠通过后续 `POST /backend-api/payments/checkout/update` 应用。
- GCash create、confirm 阶段当前强制启用 Sentinel Token 与 SO Token。

## 6. Checkout Session 类型分流

创建 Checkout 后，先识别 `checkout_session_id`：

| Session 前缀 | 分支 | 处理方式 |
|---|---|---|
| `oaics_*` | OpenAI 自定义 Checkout | 走 `fetch_custom_checkout_session`、`checkout/update`、`taxes`、method selection、confirm/start |
| `cs_live_*` 或 `cs_test_*` | 标准 Stripe Checkout | 走 `provider_checkout.stripe_to_provider()`，映射到 `external_gcash` 并返回预选官方 Checkout 链 |

如果响应里没有显式 session id，但 `url` 或响应文本中能提取到上述 id，也可以继续。

非 US 的 OAICS 默认 processor：

```text
processor_entity = checkout_data.processor_entity or "openai_ie"
```

## 7. OAICS 主流程

OAICS 是当前 GCash 的主要逻辑路径：

1. 用代理池 1 创建 `PH/PHP` OpenAI Checkout。
2. 读取 `GET /backend-api/payments/checkout/{processor_entity}/{session_id}`。
3. 递归解析支付方式，并短轮询 3 次，等待官方后端同步 GCash method。
4. 读取 `amount` 和 `currency`。
5. 如果请求了优惠，且当前 amount 不是 `None` 也不是 `0`，用代理池 2 调 `checkout/update`。
6. 优惠更新后刷新自定义 Checkout，并再次短轮询 3 次支付方式。
7. 生成 PH 账单地址，提交 `checkout/taxes`。
8. 从初始响应、优惠响应、taxes 响应、刷新响应中综合选择 GCash 方法。
9. 根据方法类型进入确认/启动分支。
10. 生成结果字段，返回跳转链接或预选 Checkout 页面。

伪代码：

```python
created = create_checkout(token, gcash_payload, entry_proxy)
session_id = created.checkout_session_id

if session_id.startswith("oaics_"):
    state = fetch_custom_checkout_session(entry_http, token, session_id, processor)
    states = [state]

    method_id = poll_select_gcash(states, times=3)
    amount = checkout_amount_minor(state)
    currency = checkout_currency(state) or "PHP"

    if use_promo and amount not in (None, 0):
        update = update_checkout_promo(promo_http, token, session_id, processor, campaign)
        states.append(update)
        state = refresh_until_method(entry_http, states, times=3)
        amount = checkout_amount_minor(state)
        currency = checkout_currency(state) or currency

    billing = default_billing("PH", account_email, real_random=True)
    tax_state = submit_custom_checkout_taxes(entry_http, token, session_id, processor, billing, currency)
    states.append(tax_state or fetch_custom_checkout_session(...))

    method_id = select_gcash_method(states)
    return handle_gcash_method(method_id, states, amount, currency)
```

## 8. 支付方式提取与选择规则

AI 不能只看顶层字段，必须递归扫描官方响应里的多种字段名：

```text
custom_payment_methods
customPaymentMethods
custom_payment_method_types
customPaymentMethodTypes
external_payment_methods
externalPaymentMethods
external_payment_method_types
externalPaymentMethodTypes
external_payment_method_specs
externalPaymentMethodSpecs
payment_method_specs
paymentMethodSpecs
payment_method_types
paymentMethodTypes
payment_methods
paymentMethods
available_payment_methods
availablePaymentMethods
available_payment_method_types
availablePaymentMethodTypes
```

单个 method 的 identifier 优先从这些字段读取：

```text
id
custom_payment_method_type_id
customPaymentMethodTypeId
payment_method_type
paymentMethodType
type
value
code
```

GCash 排名规则：

| 优先级 | 命中条件 | 返回值 |
|---:|---|---|
| 0 | 文本包含 `gcash` 且 id 是 `cpmt_*` | `cpmt_*` |
| 1 | 文本包含 `gcash` 且 method 内有 `external_*` 字段 | `external_gcash` |
| 3 | 文本包含 `gcash` 的普通 method | method id |
| 5 | 只有一个匿名 `cpmt_*`，且文本不含 blocked label | 这个 `cpmt_*` |

blocked label 包括：

```text
card, link, paypal, kakao, naver, gopay, grabpay,
paymaya, maya, pix, momo, upi, ideal, twint
```

必须拒绝的情况：

- 只有 `card,link`。
- 有多个匿名 `cpmt_*`，无法确认哪个是 GCash。
- 非 generic method 混在一起导致歧义。

可接受的 GCash 表现：

```text
payment_method_types: ["card", "gcash"] + custom_payment_methods: [{"id":"cpmt_...","display_name":"GCash"}]
externalPaymentMethods: [{"type":"external_gcash","display_name":"GCash"}]
payment_method_specs: [{"type":"gcash","display_name":"GCash"}]
payment_method_types: ["card","link"] + 唯一匿名 cpmt_*
```

## 9. 方法类型与执行分支

选中 method 后按类型分支：

### 9.1 `cpmt_*` 自定义支付方式

流程：

1. 调 `checkout/confirm`，body 类型为 `custom_payment_method`。
2. 如果 confirm 成功，再调 `custom_payment_method/start`。
3. 从 start 响应递归提取 GCash 跳转 URL。

confirm body 核心字段：

```json
{
  "checkout_session_id": "oaics_...",
  "selected_payment_method_type": "cpmt_...",
  "selectedPaymentMethodType": "cpmt_...",
  "type": "custom_payment_method",
  "custom_payment_method_type_id": "cpmt_...",
  "billing_details": {}
}
```

start body 核心字段：

```json
{
  "checkout_session_id": "oaics_...",
  "custom_payment_method_type_id": "cpmt_..."
}
```

### 9.2 `external_gcash` 外部支付方式

流程：

1. 可先尝试 `checkout/confirm`，但 confirm 被拦截或不直接放行时，不要立刻失败。
2. 继续调 `custom_payment_method/start`。
3. start 返回 `status = requires_action` 且包含跳转 URL 才算成功。

confirm body 核心字段：

```json
{
  "checkout_session_id": "oaics_...",
  "selected_payment_method_type": "external_gcash",
  "selectedPaymentMethodType": "external_gcash",
  "type": "external_payment_method",
  "external_payment_method_type": "external_gcash",
  "externalPaymentMethodType": "external_gcash"
}
```

start body 必须同时带 snake_case 和 camelCase 别名：

```json
{
  "checkout_session_id": "oaics_...",
  "custom_payment_method_type_id": "external_gcash",
  "external_payment_method_type": "external_gcash",
  "externalPaymentMethodType": "external_gcash",
  "selected_payment_method_type": "external_gcash",
  "selectedPaymentMethodType": "external_gcash"
}
```

### 9.3 原生 `gcash`

当官方只返回原生 `gcash` method type 时：

1. 从 Checkout state、update state 或 stage1 data 中读取 `publishable_key`。
2. 构造 `return_url`，优先使用官方 `confirm_return_url`，否则回退：

```text
https://chatgpt.com/checkout/verify?stripe_session_id={session_id}&processor_entity={processor}&plan_type=plus
```

3. 调 Stripe `/v1/confirmation_tokens` 创建 confirmation token。
4. 用 confirmation token 调 OpenAI `checkout/confirm`。
5. 从 confirm 响应或刷新后的 Checkout state 中提取 redirect URL。

如果缺少 `publishable_key`，必须失败：

```text
GCash 原生确认失败：Checkout 未返回 publishable_key
```

## 10. direct start 与页面降级

如果最终没有选出 `custom_method_id`：

1. 构造 `custom_url`：

```text
checkout_data.checkout_url
或 https://chatgpt.com/checkout/{processor_entity}/{session_id}
```

2. 尝试直接 start `external_gcash`。
3. 如果拿到 redirect URL，正常返回 GCash 跳转链接。
4. 如果 direct start 失败，生成预选 Checkout 页面 URL：

```text
{custom_url}?redirect_pm_type=external_gcash&ui_mode=custom&lid={uuid}
```

5. 默认不允许只返回页面降级，必须报错：

```text
GCASH_DIRECT_LINK_UNAVAILABLE:
官方 OAICS 后端仅返回 link/card，external_gcash start 被拒绝，未拿到 GCash 直跳链接
```

6. 只有满足任一条件时，才允许返回预选页面：

```text
request.allow_gcash_page_fallback = true
PAY153_ALLOW_GCASH_PAGE_FALLBACK=1
PAY153_ALLOW_GCASH_PAGE_FALLBACK=true
PAY153_ALLOW_GCASH_PAGE_FALLBACK=yes
PAY153_ALLOW_GCASH_PAGE_FALLBACK=on
```

页面降级结果必须标记：

```json
{
  "gcash_page_selection_required": true,
  "gcash_direct_start_attempted": true,
  "gcash_direct_start_failed": "error summary",
  "gcash_backend_method_missing": true,
  "gcash_backend_method_summary": "card,link"
}
```

## 11. 标准 Stripe `cs_*` 分支

如果创建得到的是 `cs_live_*` 或 `cs_test_*`：

1. GCash provider 先映射为 `external_gcash`。
2. Stripe init 后，从 `payment_method_types` 中查找 `external_gcash`，也兼容 `gcash`。
3. 如果没有找到，报错：

```text
当前 checkout 未开放 gcash/external_gcash，可用方式：...
```

4. 如果请求了优惠，则先执行 `checkout/update`，再重新 init Stripe。
5. 调 tax region 更新 PH 账单。
6. 返回官方 Checkout URL，并附加：

```text
redirect_pm_type=external_gcash
ui_mode=custom
lid={uuid}
```

该分支不尝试 `payment_method_data[type]=gcash`，因为 GCash 在 Stripe 侧以 external payment method 表达。

## 12. 金额与优惠判断

金额状态统一用：

| amount | amount_verification | promo_applied |
|---|---|---|
| `0` | `verified_zero` | `true` |
| `None` | `pending` | `null` |
| 非 0 | `nonzero` | `false` |

规则：

- `verified_zero`：优惠确认命中，今日应付为 0。
- `pending`：官方响应没有暴露金额，不能据此判定优惠失败。
- `nonzero`：优惠更新请求可能成功，但金额未归零；结果文案需要提示“优惠未生效，请确认页面金额”。
- 对 GCash 页面或 external 链接，非 0 金额不一定阻断返回，因为用户仍可能需要进入官方页面确认最终展示。

## 13. 返回结果字段

成功结果至少应包含：

```json
{
  "plan": "plus",
  "link_type": "gcash",
  "checkout_provider": "open_ai",
  "checkout_session_id": "oaics_... or cs_live_...",
  "processor_entity": "openai_ie",
  "custom_payment_method_id": "cpmt_... or external_gcash or gcash",
  "payment_method_type": "external_gcash",
  "provider_redirect_url": "https://...",
  "short_link": "https://...",
  "checkout_url": "https://...",
  "source_checkout_url": "https://chatgpt.com/checkout/...",
  "verification_url": "",
  "country": "PH",
  "currency": "PHP",
  "checkout_country": "PH",
  "checkout_currency": "PHP",
  "entry_country": "PH",
  "promo_country": "VN",
  "proxy_mode": "ph_checkout_promo_update",
  "promo_requested": true,
  "promo_applied": true,
  "promo_campaign_used": "plus-1-month-free",
  "checkout_amount": 0,
  "amount_currency": "PHP",
  "amount_verification": "verified_zero",
  "expires_at": 1800
}
```

`expires_at` 当前按生成时刻加 1800 秒。

## 14. 错误诊断表

| 错误/现象 | 含义 | AI 应采取的动作 |
|---|---|---|
| `Billing country must match request country` | Checkout request country 与 billing country 不一致 | 固定 GCash 为 `PH/PHP`，代理池 1 目标 PH，不要用 US 创建 PH 账单 |
| `methods=card,link` | 官方后端未暴露 GCash | 不要 confirm card/link；换 PH 代理或跑矩阵探测 |
| `GCASH_DIRECT_LINK_UNAVAILABLE` | 没有方法，`external_gcash` start 也失败，且页面降级未开启 | 返回失败；建议换代理/地区矩阵，或显式开启 page fallback |
| `CUSTOM_CONFIRM_BLOCKED` | OpenAI checkout confirm 被上游拦截 | 对非 external method 可短暂停顿后重试一次；仍失败则停止 |
| `启动 GCash 支付失败 HTTP ...` | `custom_payment_method/start` 被拒绝 | 记录摘要；若无 fallback 权限则失败 |
| `GCash 未返回跳转链接` | start 响应不是 `requires_action` 或没有 URL | 失败或页面降级 |
| `优惠金额校验失败：Stripe 未返回今日应付金额` | 标准 Stripe 分支无法读取 amount | 视作金额待复核；不要把它写成优惠成功 |
| `checkout/update HTTP != 200` | 优惠更新接口失败 | 当前尝试失败，外层可换代理重试 |
| `amount` 非 0 | 优惠未命中 | 可以返回链，但必须标注 `promo_applied=false` 与提示文案 |

## 15. 探测流程

当真实任务多次只返回 `card,link` 时，先跑只读探测，不要直接 confirm。

小流量 PH 基线：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --paired-countries PH `
  --ui-modes redirect,custom `
  --no-promo `
  --limit 2 `
  --jsonl logs\gcash-no-promo-baseline.jsonl
```

PH Checkout + 默认 VN 优惠更新：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --checkout-countries PH `
  --proxy-countries PH,SG,MY,ID,TH,VN,US `
  --profile-countries PH `
  --ui-modes redirect,custom `
  --promo-country VN `
  --limit 14 `
  --jsonl logs\gcash-country-probe.jsonl
```

判定：

- `FOUND ... methods=...gcash... selected=... amount=0 PHP`：可固定该组合进入主流程。
- `FOUND ... amount=非0 PHP`：GCash 可用，但优惠未命中。
- `MISS ... methods=card,link`：该组合没有暴露 GCash。
- `amount=-` 或 JSONL 中 `amount=null`：金额未知，不能单独判定失败。

## 16. 推荐 AI 系统提示词

可以把下面这一段作为服务器端 AI 的系统/开发提示词基础：

```text
你是 PAY153 的 GCash 提链执行代理。你的任务是根据输入 token、代理池和优惠参数，生成 GCash 官方支付跳转链接或明确失败原因。

硬性规则：
1. GCash 固定使用 PH/PHP Checkout。不要用 US 创建 PH 账单。
2. 代理池 1 用于创建 Checkout、读取 OAICS、提交 PH taxes、confirm 和 start；代理池 2 只用于优惠预检和 checkout/update。
3. 创建 Checkout 使用 checkout_ui_mode=redirect；GCash 默认不在 create 阶段携带 promo_campaign。
4. Stripe canonical method 是 external_gcash；同时兼容 gcash 和 cpmt_*。
5. 只有 card/link 时不得误判为 GCash，不得继续 confirm card/link。
6. external_gcash 走 custom_payment_method/start，不走 Stripe confirmation_token。
7. 原生 gcash 必须先创建 Stripe confirmation_token，再调用 checkout/confirm。
8. 优惠是否命中以 amount 为准：0=verified_zero，None=pending，非0=nonzero。
9. 无 direct GCash URL 时，只有 allow_gcash_page_fallback 或 PAY153_ALLOW_GCASH_PAGE_FALLBACK 开启，才能返回预选 Checkout 页面；否则报 GCASH_DIRECT_LINK_UNAVAILABLE。
10. 任何日志和回答都必须脱敏 token、cookie、代理密码、pk_live、cuss_secret、cf_clearance。

执行顺序：
1. 解析 token/session JSON，保留 Bearer token、邮箱、账号 id、ChatGPT cookie 元数据。
2. 固定 country=PH、currency=PHP、checkout_country=PH、checkout_currency=PHP。
3. 使用代理池 1 创建 OpenAI Checkout。
4. 若返回 oaics_*，读取 custom checkout，必要时用代理池 2 执行 checkout/update，再用代理池 1 提交 PH taxes。
5. 从所有响应状态递归选择 GCash method，优先 cpmt_*，其次 external_gcash，再其次原生 gcash；拒绝 card/link-only。
6. 根据 method 类型 confirm/start，提取 redirect URL。
7. 若返回 cs_*，走 Stripe init，查找 external_gcash/gcash，应用优惠后返回带 redirect_pm_type=external_gcash 的 Checkout 链。
8. 输出结构化结果：provider_redirect_url、payment_method_type、checkout_amount、amount_verification、promo_applied、processor_entity、诊断字段。
9. 失败时输出稳定错误码和建议动作，不暴露敏感原文。
```

## 17. 最小决策树

```text
Start
  |
  |-- Force PH/PHP, entry=PH, promo=VN or user country
  |
  |-- Create OpenAI Checkout
        |
        |-- oaics_*
        |     |
        |     |-- fetch custom state -> maybe promo update -> submit PH taxes
        |     |
        |     |-- select GCash method
        |           |
        |           |-- cpmt_* -> confirm custom -> start custom -> redirect URL
        |           |-- external_gcash -> start external -> redirect URL
        |           |-- gcash -> Stripe confirmation_token -> OpenAI confirm -> redirect URL
        |           |-- none -> try start external_gcash
        |                    |
        |                    |-- URL -> success
        |                    |-- no URL + fallback allowed -> preselected Checkout page
        |                    |-- no URL + fallback denied -> GCASH_DIRECT_LINK_UNAVAILABLE
        |
        |-- cs_*
              |
              |-- Stripe init -> find external_gcash/gcash
              |-- optional promo update -> re-init
              |-- tax region -> return hosted URL with redirect_pm_type=external_gcash
```

## 18. 回归检查清单

部署 AI 或改写逻辑后，至少验证：

- `gcash` 默认 provider 是 `PH/PHP`。
- `default_entry_proxy_country("gcash", "PH") == "PH"`。
- `default_exit_proxy_country("gcash", "PH", "", true) == "VN"`。
- GCash payload 的 `checkout_ui_mode == "redirect"`。
- GCash payload 默认不包含 `promo_campaign`。
- `card,link` 不会被选成 GCash。
- 单个匿名 `cpmt_*` 可以被选中，但多个匿名 `cpmt_*` 必须拒绝。
- `external_gcash` start body 同时包含 snake_case 和 camelCase 字段。
- `gcash_preselected_checkout_url()` 会添加 `redirect_pm_type=external_gcash`、`ui_mode=custom` 和 `lid`。
- page fallback 默认关闭。
- `amount=0/None/非0` 分别得到 `verified_zero/pending/nonzero`。

## 19. 跨电脑 AI 重写 Web 应用规格

如果另一个 AI 要在新服务器上重写一个链路一致的 Web 应用，它需要实现三个层次：

| 层 | 必须实现 | 可以简化 |
|---|---|---|
| 前端 | 表单提交、进度轮询、结果展示、错误展示、取消任务 | UI 样式、主题、动画 |
| API 层 | 创建任务、查询任务、取消任务、配置接口、健康检查 | 私有通道、旧服务兼容、批量任务 |
| GCash 服务层 | token 解析、代理选择、Checkout 创建、OAICS/Stripe 分流、优惠更新、PH taxes、method selection、confirm/start、金额判断 | 其他支付方式 |

最小可用 Web 应用只需要支持 `link_type=gcash`，但接口字段和结果字段应与本文保持一致，方便前端、批处理或第三方调用复用。

推荐目录结构：

```text
gcash-web/
├─ app.py                    # Flask/FastAPI 入口、API 路由、任务队列
├─ gcash_service.py          # GCash 核心链路
├─ openai_checkout.py        # ChatGPT/OpenAI Checkout 请求封装
├─ stripe_client.py          # Stripe init、confirmation_token、elements 辅助
├─ billing.py                # PH 账单地址生成
├─ proxy_utils.py            # 代理规范化、地区提示、代理选择
├─ sentinel.py               # Sentinel/SO token 生成或外部调用封装
├─ static/
│  ├─ index.html             # 单页表单
│  ├─ app.js                 # 提交、轮询、展示
│  └─ styles.css
└─ tests/
   ├─ test_gcash_routing.py
   ├─ test_method_selection.py
   └─ test_api_contract.py
```

## 20. Web API 契约

### 20.1 `GET /api/health`

返回：

```json
{
  "ok": true,
  "service": "gcash-web",
  "time": 1800000000
}
```

### 20.2 `GET /api/config`

最小返回：

```json
{
  "plans": ["plus"],
  "link_types": ["gcash"],
  "provider_defaults": {
    "gcash": {"country": "PH", "currency": "PHP"}
  },
  "proxy_policy": {
    "entry_required": true,
    "exit_required_for": ["gcash"],
    "max_per_pool": 500,
    "selection": "random_per_job"
  },
  "retry_policy": {
    "min": 1,
    "max": 10,
    "default_gcash": 10
  }
}
```

### 20.3 `POST /api/checkout`

创建异步任务。请求体：

```json
{
  "token": "access token or session json",
  "plan": "plus",
  "link_type": "gcash",
  "country": "PH",
  "currency": "PHP",
  "entry_proxies": ["http://user:pass@ph-proxy:port"],
  "exit_proxies": ["http://user:pass@vn-or-promo-proxy:port"],
  "retry_count": 10,
  "use_promo": true,
  "promo_campaign": "plus-1-month-free",
  "promo_country": "VN",
  "use_sen": true,
  "use_so": true,
  "allow_gcash_page_fallback": false
}
```

兼容字段：

```text
entry_proxy / api_proxy / proxy -> entry_proxies
exit_proxy / payment_proxy -> exit_proxies
```

服务端必须归一化：

```text
plan = "plus"
link_type = "gcash"
country = "PH"
currency = "PHP"
checkout_country = "PH"
checkout_currency = "PHP"
entry_proxy_country = request.entry_proxy_country or "PH"
exit_proxy_country = request.exit_proxy_country or request.promo_country or "VN"
retry_count = clamp(request.retry_count or 10, 1, 10)
use_promo = true only when plan == "plus"
```

成功响应：

```json
{
  "ok": true,
  "job_id": "16-char-or-uuid",
  "queue_position": 0,
  "global_rpm": 20,
  "ip_rpm": 3,
  "internal": false
}
```

常见失败响应：

```json
{"error": "请填写 Access Token 或 Session JSON"}
{"error": "请填写 Checkout 入口代理"}
{"error": "当前支付路径需要填写支付出口代理"}
{"error": "入口代理至少填写 1 条"}
{"error": "出口代理至少填写 1 条"}
{"error": "重试次数需要填写 1-10 的整数"}
```

### 20.4 `GET /api/checkout-progress?job_id=...`

返回任务快照：

```json
{
  "id": "job_id",
  "status": "queued | running | done | error | cancelled",
  "percent": 72,
  "text": "正在提交 PH 账单地址",
  "logs": [
    {"time": "12:00:00", "message": "GCash 路由：Checkout=PH，账单=PH/PHP，优惠更新=VN", "major": true}
  ],
  "result": null,
  "error": "",
  "last_retry_error": "",
  "queue_position": 0,
  "created_at": 1800000000,
  "updated_at": 1800000001
}
```

终态 `done` 时 `result` 必须是第 13 节结构；终态 `error` 时 `error` 必须包含稳定错误摘要。

### 20.5 `POST /api/checkout-cancel`

请求：

```json
{"job_id": "job_id"}
```

返回：

```json
{"ok": true}
```

取消后任务状态：

```json
{
  "status": "cancelled",
  "percent": 100,
  "text": "任务已停止",
  "error": "任务已停止"
}
```

## 21. 后台任务状态机

最小状态机：

```text
queued
  -> running
      -> done
      -> error
      -> cancelled
```

推荐进度点：

| percent | text | 说明 |
|---:|---|---|
| 2 | 任务已创建 | API 已接受任务 |
| 4 | 正在准备任务 | 选择代理、解析参数 |
| 9 | 校验 PH Checkout 与优惠代理 | 校验代理地区或记录提示 |
| 12 | 读取入口支付与活动标记 | 优惠预检 |
| 18 | 生成 Sentinel 校验 | 准备 create checkout |
| 34 | 创建 OpenAI Checkout | 调 OpenAI checkout |
| 44 | Checkout 创建完成，正在准备支付方式 | 拿到 session |
| 58 | 正在读取 GCash 自定义支付方式 | OAICS fetch |
| 66 | 正在应用优惠并刷新 GCash Checkout | checkout/update |
| 72 | 正在提交 PH 账单地址 | taxes |
| 76 | 正在确认 GCash 支付方式 | confirm |
| 82 | 正在直连 GCash 支付 | external direct start |
| 88 | 正在生成 GCash 跳转链接 | start/extract redirect |
| 100 | GCash 跳转链接生成完成 | done |

任务并发规则：

- 同一个 `token_raw` 同时只允许一个任务运行；并发创建 Checkout 会让旧 session 失效。
- 每个任务最多重试 `retry_count` 次。
- GCash 默认重试 10 次。
- 每次重试重新选择代理对和新的 device id。
- 遇到 token 过期、任务停止、确认被永久 blocked 等非重试错误，应停止重试。

## 22. 服务端模块函数契约

新实现不必逐字复制原代码，但函数语义必须一致。

### 22.1 配置与归一化

```python
PROVIDER_DEFAULTS = {
    "gcash": {"country": "PH", "currency": "PHP"}
}

def default_entry_proxy_country(link_type: str, country: str) -> str:
    if link_type == "gcash":
        return "PH"
    return country or "US"

def default_exit_proxy_country(link_type: str, country: str, promo_country: str, use_promo: bool) -> str:
    if link_type == "gcash":
        return promo_country or "VN"
    return country or "US"

def stripe_payment_method_type(provider: str) -> str:
    return "external_gcash" if provider == "gcash" else provider
```

### 22.2 OpenAI Checkout

```python
def create_checkout(token, payload, proxy, device_id, did, use_sen=True, use_so=True, credential_meta=None) -> dict:
    """
    POST OpenAI checkout endpoint.
    Return:
      {
        "data": {
          "checkout_session_id": "oaics_* or cs_*",
          "checkout_url": "...",
          "processor_entity": "openai_ie",
          "checkout_provider": "open_ai",
          "publishable_key": "pk_live_..."
        },
        "http": warmed_chatgpt_http_session
      }
    """
```

必须实现：

- 先访问 `https://chatgpt.com/api/auth/csrf` 暖身 cookie。
- 创建时发送 Bearer token、ChatGPT cookie、`OAI-Device-Id`、Sentinel/SO header。
- 从响应字段、URL、原始文本中提取 `cs_*` 或 `oaics_*`。
- 若为 `oaics_*`，构造 `https://chatgpt.com/checkout/{processor}/{oaics_id}`。

### 22.3 OAICS 请求

```python
def fetch_custom_checkout_session(http, token, session_id, processor_entity, device_id) -> dict: ...

def update_checkout_promo(http, token, session_id, processor_entity, campaign_id, device_id) -> dict: ...

def submit_custom_checkout_taxes(http, token, session_id, processor_entity, billing, currency, device_id) -> dict: ...

def confirm_custom_checkout_method(http, token, session_id, processor_entity, method_id, proxy, device_id, did, confirmation_token="", billing=None) -> dict: ...

def start_custom_checkout_method(http, token, session_id, processor_entity, method_id, device_id) -> dict: ...
```

接口路径：

```text
GET  https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{session_id}
POST https://chatgpt.com/backend-api/payments/checkout/update
POST https://chatgpt.com/backend-api/payments/checkout/taxes
POST https://chatgpt.com/backend-api/payments/checkout/confirm
POST https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start
```

每个请求至少带：

```text
Authorization: Bearer {token}
Content-Type: application/json
Accept: application/json 或 */*
Origin: https://chatgpt.com
Referer: https://chatgpt.com/checkout/{processor_entity}/{session_id}
User-Agent: Chrome-like UA
OAI-Device-Id: {device_id}
```

confirm 请求额外需要 Sentinel/SO：

```text
OpenAI-Sentinel-Token: {...}
OpenAI-Sentinel-SO-Token: {...}
OAI-Telemetry: [1,null]
```

## 23. 前端最小行为

前端只需要一个页面：

输入控件：

- Access Token / Session JSON 文本框
- 计划选择，最小只保留 Plus
- 支付方式选择，最小只保留 GCash
- 代理池 1 文本框
- 代理池 2 文本框
- 重试次数
- 是否使用优惠
- 优惠 campaign

提交逻辑：

```javascript
const body = {
  token,
  plan: "plus",
  link_type: "gcash",
  country: "PH",
  currency: "PHP",
  entry_proxies: entryProxyLines,
  exit_proxies: exitProxyLines,
  retry_count: retryCount,
  use_promo: usePromo,
  promo_campaign: promoCampaign || "plus-1-month-free",
  promo_country: promoCountry || "VN",
  use_sen: true,
  use_so: true
};

POST /api/checkout -> {job_id}
setInterval(() => GET /api/checkout-progress?job_id=..., 1200)
```

结果展示优先级：

```text
short_link
provider_redirect_url
verification_url
checkout_url
```

优惠提示：

- `promo_requested=false`：显示未请求。
- `promo_applied=true`：显示已生效、今日应付 0。
- `promo_applied=false` 或 `amount_verification=nonzero`：醒目提示优惠未生效，需要确认页面金额。
- `amount_verification=pending`：显示金额待页面复核。

## 24. 重写验收标准

让另一台电脑上的 AI 重写完成后，必须通过以下验收。没有真实 token 和代理时，至少用 mock response 测试。

### 24.1 单元测试

```python
def test_gcash_defaults():
    assert PROVIDER_DEFAULTS["gcash"] == {"country": "PH", "currency": "PHP"}
    assert default_entry_proxy_country("gcash", "PH") == "PH"
    assert default_exit_proxy_country("gcash", "PH", "", True) == "VN"

def test_gcash_payload():
    payload = checkout_payload({"plan":"plus","link_type":"gcash","country":"PH","currency":"PHP","use_promo":True}, {})
    assert payload["checkout_ui_mode"] == "redirect"
    assert "promo_campaign" not in payload

def test_reject_card_link_only():
    payload = {"checkout_session": {"payment_method_types": ["card", "link"]}}
    assert select_custom_checkout_method_from_states("gcash", payload) == ""

def test_external_gcash_start_body_aliases():
    body = build_start_body("oaics_123", "external_gcash")
    assert body["custom_payment_method_type_id"] == "external_gcash"
    assert body["external_payment_method_type"] == "external_gcash"
    assert body["externalPaymentMethodType"] == "external_gcash"
    assert body["selected_payment_method_type"] == "external_gcash"
    assert body["selectedPaymentMethodType"] == "external_gcash"
```

### 24.2 Mock 集成测试

必须模拟并通过这些场景：

| 场景 | Mock 响应 | 期望 |
|---|---|---|
| OAICS + `cpmt_*` GCash | fetch 返回 `custom_payment_methods=[{"id":"cpmt_x","display_name":"GCash"}]` | confirm 后 start，返回 redirect |
| OAICS + `external_gcash` | fetch 返回 `externalPaymentMethods=[{"type":"external_gcash","display_name":"GCash"}]` | start body 带 external aliases |
| OAICS + 原生 `gcash` | fetch 返回 `payment_method_specs=[{"type":"gcash"}]` 和 `publishable_key` | 创建 confirmation_token，再 confirm |
| OAICS 只有 `card,link` | fetch/update/taxes 都只有 `card,link` | direct start `external_gcash`；失败时默认报 `GCASH_DIRECT_LINK_UNAVAILABLE` |
| 页面降级开启 | direct start 失败，fallback=true | 返回带 `redirect_pm_type=external_gcash` 的 Checkout URL |
| 标准 `cs_live_*` | Stripe init 返回 `payment_method_types=["card","external_gcash"]` | 返回 hosted URL，带 `redirect_pm_type=external_gcash` |
| 优惠非 0 | amount=110000 | `promo_applied=false`，文案包含优惠未生效 |
| 优惠 0 | amount=0 | `promo_applied=true`，`amount_verification=verified_zero` |
| 金额缺失 | amount=None | `amount_verification=pending`，不宣称优惠成功 |

### 24.3 真实环境 smoke test

真实环境只建议先跑只读探测：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --paired-countries PH `
  --ui-modes redirect,custom `
  --no-promo `
  --limit 2 `
  --jsonl logs\gcash-smoke.jsonl
```

确认出现 `FOUND` 后，再跑真实 Web 任务。

## 25. 给异地 AI 的重写提示词

如果你要把任务交给另一台电脑上的 AI，推荐直接贴下面这段：

```text
请根据这份 Markdown 从零实现一个最小可用的 GCash 提链 Web 应用。要求：

1. 只实现 GCash，不需要实现 PayPal、PIX、UPI、Kakao 等其他支付方式。
2. 后端使用 Python Flask 或 FastAPI；前端使用原生 HTML/CSS/JS 即可。
3. 必须提供：
   - GET /api/health
   - GET /api/config
   - POST /api/checkout
   - GET /api/checkout-progress?job_id=...
   - POST /api/checkout-cancel
4. GCash 链路必须与本文一致：
   - PH/PHP Checkout
   - 代理池 1 创建/读取/taxes/confirm/start
   - 代理池 2 preflight/update promo
   - checkout_ui_mode=redirect
   - 不在 create 阶段默认携带 promo_campaign
   - oaics_* 和 cs_* 分支分流
   - external_gcash/cpmt_*/gcash 三种方法分别处理
   - card/link-only 必须拒绝
   - direct start 失败时，默认报 GCASH_DIRECT_LINK_UNAVAILABLE，除非 fallback 显式开启
5. 输出字段必须包含 provider_redirect_url、short_link、checkout_amount、amount_verification、promo_applied、processor_entity、payment_method_type、诊断字段。
6. 日志必须脱敏 token、cookie、代理密码、pk_live、cuss_secret、cf_clearance。
7. 请先写 mock 单元测试，覆盖第 24 节所有场景，再实现后端和前端。
8. 不要用浏览器自动化作为主链路；主链路必须通过 HTTP API 完成。
```
