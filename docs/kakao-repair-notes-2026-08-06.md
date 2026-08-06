# Kakao Pay 修复记录 - 2026-08-06

## 当前结论

本轮先暂停继续尝试。最新失败已经不是“页面没点 Kakao”或“账单地址没填”的问题。

最新日志：`logs/2026-08-06/04439c82f2f54cbf.log`

最新卡点：

```text
[stripe] init ok ... amount=0 currency=krw pm=['card', 'kakao_pay', 'naver_pay']
[promo] Plus 首月免费校验通过：Stripe 今日应付 amount=0
[stripe] snapshot billing: 204
[stripe] kakao_pay confirm strategy=inline zero_due=True ...
[kakao] confirm 返回：submission_state=requires_approval ...
[stripe] manual_approval approve+sentinel: 200 {"result":"blocked"}
错误：RuntimeError: manual_approval approve blocked: result=blocked
```

这说明：

- 优惠已经生效，今日应付为 `0 KRW`。
- Kakao Pay 支付方式可用，并且已经进入 Stripe confirm。
- KR 账单地址已提交并 snapshot 成功。
- 真正失败点是 ChatGPT 后端的 manual approval：`/backend-api/payments/checkout/approve` 返回 `blocked`。

## 已修复内容

### GCash - 2026-08-06 23:10

最新 GCash 失败日志：`logs/2026-08-06/84ed7b829c8d4714.log`

失败点：

```text
创建 OpenAI Checkout
OpenAI Checkout HTTP 400: {"detail":"Billing country must match request country."}
```

结论：

- 旧路由是“US 入口创建 PH/PHP 账单，再用 VN/优惠国家更新优惠”。
- 当前上游在 checkout create 阶段已经强校验：request country 必须与 billing country 一致。
- 所以 GCash 不能再默认用 US 创建 PH 账单，应改为：
  - 代理池 1：PH，创建 PH/PHP Checkout，并用于 GCash 确认。
  - 代理池 2：VN 或用户选择的优惠国家，仅用于优惠预检/更新。

已改：

- `app.py`：`default_entry_proxy_country("gcash", "PH")` 改为 `PH`。
- `app.py`：GCash 日志和 `proxy_mode` 从 US checkout 改为 PH checkout。
- `app.py`：GCash 创建阶段改为 `checkout_ui_mode=redirect`，优先拿 `cs_live` 标准 Stripe Checkout，再像 Kakao 一样通过 Stripe init / `payment_method_types` 找支付方式。
- `app.py`：GCash 标准 Stripe init / confirm 使用 PH 代理池 1，VN/用户选择地区只用于优惠更新。
- `app.py`：GCash 支付方式选择改为递归读取 checkout/update/taxes 响应，不再只看顶层 `custom_payment_methods`。
- `app.py`：支付方式提取器新增 `external_payment_methods` / `externalPaymentMethods` 等官方新字段名兼容。
- `app.py`：GCash 若只返回原生 `gcash` method type，则走 Stripe `confirmation_token` + OpenAI confirm，不再误判为没有支付方式。
- `app.py`：GCash 候选只有 `card,link` 时不再误选 `card`，避免触发 `payment_method_data[card]` 缺失的 Stripe 400。
- `static/app.js`：前端 GCash 推荐提示改为“代理池 1 使用 PH”。
- `static/index.html`：更新 `app.js` cachebuster，避免浏览器继续用旧提示。
- `tests/test_checkout_routing.py`：新增回归测试，防止 GCash 默认入口又回退到 US，并覆盖嵌套 update payload 里的 GCash method 和 `card,link` 拒选。

验证：

```text
.venv\Scripts\python.exe -m unittest tests.test_checkout_routing tests.test_provider_mapping
Ran 36 tests - OK
```

### Kakao Pay

1. 补齐 Chrome 9222 登录态导入

- 从 9222 读取 ChatGPT access token、Cookie、UA、语言等环境。
- 避免只拿 bearer token 导致后端判断环境不完整。
- 相关文件：
  - `app.py`
  - `tools/chrome_cdp_session.js`
  - `tools/chrome_cdp_fetch.js`

2. 补齐 OAICS / Kakao confirm 字段

- Kakao 原生流程使用 Stripe `confirmation_token`。
- confirm body 兼容新旧字段：
  - `confirm_token`
  - `confirmToken`
  - `confirmation_token`
  - `selected_payment_method_type`
  - `selectedPaymentMethodType`
  - `type: confirmation_token`
- 相关文件：
  - `app.py`
  - `tests/test_checkout_routing.py`

3. 修复 Kakao return_url

- 优先从 checkout state 读取 `confirm_return_url`。
- fallback 到：

```text
https://chatgpt.com/checkout/verify?stripe_session_id=...&processor_entity=...&plan_type=plus
```

4. 补齐 KR 账单地址校验

- Kakao / KR 表单要求：
  - name
  - country
  - state，道/县，例如 `Seoul`
  - city
  - line1
  - postal_code
- 缺 `state` 时提前报“账单地址不完整”，不再拖到 confirm 阶段。

5. Chrome 页面可见状态辅助

- CDP confirm helper 可在 confirm 前点选 Kakao tile，让可见页面和后台选择尽量一致。
- 这只是辅助选择支付方式，不会点击“订阅”按钮。

6. 新增/更新测试

当前通过：

```text
.venv\Scripts\python.exe -m unittest tests.test_checkout_routing tests.test_provider_mapping
Ran 31 tests - OK
```

编译和 JS 检查：

```text
compile ok
node --check tools\chrome_cdp_fetch.js
```

## 仍未解决

最终 `approve` 返回 `blocked`。

目前已排除或基本排除：

- 不是优惠未生效，日志已确认 amount=0。
- 不是 Kakao 不可用，日志已确认 `kakao_pay` 在 payment methods 中。
- 不是普通账单地址缺失，tax region 和 snapshot 都成功。
- 不是单纯页面 tile 未选中，后台流程已选中并进入 confirm。

剩余可能：

- ChatGPT manual approval 风控/资格拒绝。
- 账号、IP、代理线路、Sentinel、Cookie、设备态之间仍有不一致。
- 当前优惠或零元 Kakao Pay 对该账号/地区组合被后端策略拦截。
- Stripe confirm 成功进入 `requires_approval`，但 ChatGPT approve 层不放行。

## 关于登录态

真实端到端测试目前仍然需要登录态。

原因是这些接口都不是公开接口：

- `/api/auth/session`
- `/backend-api/payments/checkout/...`
- `/backend-api/payments/checkout/update`
- `/backend-api/payments/checkout/approve`
- Sentinel 相关接口
- 账号活动目录和优惠资格接口

如果没有登录态，只能测到代码拼参、路由、mock 响应，无法确认真实上游是否放行。

但后续不应该每次调试都依赖手工登录。建议拆成三层测试：

1. 无登录态单元测试

- 覆盖参数构造、账单地址、支付方式映射、return_url、confirm body。
- 当前已经有一部分在 `tests/test_checkout_routing.py`。

2. 无登录态回放测试

- 保存脱敏后的真实响应 fixture。
- 用 fake HTTP session 重放：
  - checkout create
  - promo update
  - Stripe init
  - tax update
  - snapshot
  - confirm
  - approve blocked/success
- 这样可以复现 `manual_approval approve blocked` 分支，不需要登录。

3. 有登录态冒烟测试

- 只在需要验证真实上游时使用 Chrome 9222。
- 只跑少量轮次，比如 1-2 次。
- 输出诊断：优惠是否生效、是否进入 requires_approval、approve result 是什么。

## 建议下次继续的方向

优先做“无登录态回放测试/诊断模式”，不要继续盲目换代理跑。

建议新增：

- `PAY153_DRY_RUN_APPROVE=1`
  - 跑到 `requires_approval` 后停止，不真实调用 approve。
  - 输出 checkout/session/amount/payment_method/billing/sentinel 环境摘要。

- `tests/fixtures/kakao_approve_blocked.json`
  - 存脱敏日志/响应。

- `tests/test_kakao_workflow_replay.py`
  - 回放完整 Kakao 零元优惠流程。
  - 断言最终卡点是 approve blocked，而不是地址、优惠、Kakao 选择或 confirm body。

如果之后还要打真实链路，建议只验证一个问题：

```text
相同账号 + 相同 Chrome 登录态 + 不同 approve 线路，approve 是否仍 blocked
```

如果仍 blocked，就基本可以认定是上游 approval 策略拒绝，不是本地字段问题。

## GCash 追加记录

### 当前卡点

GCash 已经修到 PH/PHP Checkout、VN 优惠更新这条路，但真实返回仍多次是：

```text
checkout_session_id=oaics_...
候选支付方式=link,card
GCASH_METHOD_UNAVAILABLE
```

这不是继续看 Chrome 登录态能解决的问题。下一步要先确认官方到底在哪个组合返回 GCash：

- Checkout 账单国家/币种，例如 PH/PHP。
- Checkout 代理国家，例如 PH/SG/MY/ID/TH/VN/US。
- Checkout UI 模式，`redirect` 和 `custom` 都要探。
- Stripe profile locale/timezone，尤其 PH 是否需要 `en-PH` / `Asia/Manila`。
- 优惠更新仍按 VN 代理跑。

### 已补的低风险修复

- GCash 解析现在会识别 `payment_method_specs` / `paymentMethodSpecs`。
- 同时识别 `external_payment_method_specs` / `externalPaymentMethodSpecs`。
- 这样如果官方把 GCash 从 `payment_method_types` 挪到 specs 结构里，不会再误判成只有 card/link。
- Stripe 官方 external payment method 名称为 `external_gcash`。
- `external_gcash` 不能走 Stripe `confirmation_token`；本地已改为 external/custom start 路径。
- 标准 `cs_live_*` 分支也已把 `gcash` 映射为 `external_gcash`；检测到该方式时返回官方 external Checkout 链，不再尝试 `payment_method_data[type]=gcash`。

### 新增探测脚本

脚本：`tools/gcash_country_probe.py`

特点：

- 不点击页面。
- 不 confirm。
- 不 start payment method。
- 只创建 Checkout、可选执行 promo update 和 PH taxes，用来读支付方式集合。
- 输出会脱敏 token、cookie、proxy password、publishable key 等敏感信息。
- 同时解析 `oaics_*` 自定义 Checkout 和 `cs_live_*` 标准 Stripe Checkout。

建议先跑小矩阵：

```text
.venv\Scripts\python.exe tools\gcash_country_probe.py ^
  --token-file <token-or-session-json.txt> ^
  --proxy-file <gcash-proxies.txt> ^
  --checkout-countries PH ^
  --proxy-countries PH,SG,MY,ID,TH,VN,US ^
  --ui-modes redirect,custom ^
  --profile-countries PH ^
  --promo-country VN ^
  --limit 14 ^
  --jsonl logs\gcash-country-probe.jsonl
```

如果任一行出现：

```text
FOUND ... methods=...gcash...
```

再把主流程固定到那组 `checkout_country/proxy_country/ui_mode/profile_country`。

如果所有行仍是：

```text
MISS ... methods=link,card
```

就继续扩大国家矩阵，而不是继续改 confirm 或登录态。
