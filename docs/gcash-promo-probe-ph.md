# GCash PH 优惠探测手册

本文用于探测 GCash 在 `PH` 优惠地区下是否可用，以及优惠更新后金额是否归零。

探测脚本是：

```text
tools/gcash_country_probe.py
```

它只会创建 Checkout、读取支付方式、可选执行 `checkout/update` 优惠更新和 taxes 地址步骤，不会 confirm，也不会 start 支付方式。

## 最小探测命令

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --checkout-countries PH `
  --proxy-countries PH `
  --profile-countries PH `
  --ui-modes redirect,custom `
  --promo-country PH `
  --jsonl logs\gcash-promo-ph-probe.jsonl
```

参数含义：

- `--checkout-countries PH`：创建 PH/PHP Checkout。
- `--proxy-countries PH`：创建 Checkout 使用 PH 代理。
- `--profile-countries PH`：Stripe profile 使用 `en-PH` / `Asia/Manila`。
- `--promo-country PH`：优惠更新 `checkout/update` 使用 PH 代理。
- `--ui-modes redirect,custom`：同时探测两种官方 UI 模式。
- `--jsonl`：保存每一行探测结果，方便复盘。

如果只想先小流量跑第一条代理：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --paired-countries PH `
  --ui-modes redirect,custom `
  --promo-country PH `
  --limit 2 `
  --jsonl logs\gcash-promo-ph-smoke.jsonl
```

## 对照探测

建议先跑一次不带优惠的基线：

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

再跑 PH 优惠：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --paired-countries PH `
  --ui-modes redirect,custom `
  --promo-country PH `
  --limit 2 `
  --jsonl logs\gcash-promo-ph-smoke.jsonl
```

对比两份 JSONL：

- 基线存在 GCash，PH 优惠后也存在 GCash：支付方式没有被优惠更新打掉。
- PH 优惠后 `amount=0 PHP`：优惠命中。
- PH 优惠后 `amount` 仍为非 0：GCash 可用，但优惠未命中。
- PH 优惠后 `amount` 为空：官方当前响应没有暴露金额，不能仅凭这一项判定失败，要结合真实任务或后续响应字段继续确认。

## 输出判定

成功识别支付方式时，终端会出现类似：

```text
FOUND checkout=PH/PHP proxy=PH promo=PH ui=redirect profile=PH sid=oaics_... kind=oaics
  methods=link,card,cpmt_1TOgstC6h1nxGoI3WUVEY2cJ selected=cpmt_1TOgstC6h1nxGoI3WUVEY2cJ amount=0 PHP
```

重点看三项：

- `FOUND`：探测到 GCash 相关支付方式。
- `selected=cpmt_...`：新版官方接口常用匿名 `cpmt_*` 表示 GCash custom method。
- `amount=0 PHP`：优惠金额已归零。

失败或未命中示例：

```text
MISS checkout=PH/PHP proxy=PH promo=PH ui=redirect profile=PH sid=oaics_... kind=oaics
  methods=card,link selected=- amount=-
```

这表示当前组合没有暴露 GCash，优先换代理或扩大矩阵，不要继续跑 confirm。

## 查看 JSONL 摘要

用 PowerShell 快速查看结果：

```powershell
Get-Content logs\gcash-promo-ph-probe.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Select-Object checkout_country,proxy_country,promo_country,ui_mode,selected_gcash_method,amount,amount_currency,error |
  Format-Table -Auto
```

只看已选中 GCash 的行：

```powershell
Get-Content logs\gcash-promo-ph-probe.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.selected_gcash_method } |
  Select-Object checkout_country,proxy_country,promo_country,ui_mode,method_summary,selected_gcash_method,amount,amount_currency |
  Format-Table -Auto
```

只看金额归零的行：

```powershell
Get-Content logs\gcash-promo-ph-probe.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.amount -eq 0 } |
  Select-Object checkout_country,proxy_country,promo_country,ui_mode,selected_gcash_method,amount,amount_currency |
  Format-Table -Auto
```

## 扩大矩阵

如果 PH/PH 没有命中，可以保留 PH 优惠地区，只扩大 Checkout 代理出口：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --checkout-countries PH `
  --proxy-countries PH,SG,MY,ID,TH,VN `
  --profile-countries PH `
  --ui-modes redirect,custom `
  --promo-country PH `
  --jsonl logs\gcash-promo-ph-matrix.jsonl
```

如果要严格探测“国家三件套一致”，用：

```powershell
.\.venv\Scripts\python.exe tools\gcash_country_probe.py `
  --token-file data\session.txt `
  --proxy-file data\gcash-proxies.txt `
  --paired-countries PH,SG,MY,ID,TH,VN `
  --ui-modes redirect,custom `
  --promo-country PH `
  --jsonl logs\gcash-promo-ph-paired.jsonl
```

## 主流程固定方式

探测确认 PH 优惠可用后，正式任务建议：

- 支付方式：`gcash`
- 计划：`plus`
- Checkout 地区：`PH`
- Checkout 币种：`PHP`
- 代理池 1：PH，用于创建 Checkout、读取 GCash、提交 PH 账单、确认支付。
- 代理池 2：PH，用于 `checkout/update` 优惠更新。
- 优惠：开启，campaign 默认 `plus-1-month-free`。

API 请求字段示例：

```json
{
  "plan": "plus",
  "link_type": "gcash",
  "country": "PH",
  "currency": "PHP",
  "checkout_country": "PH",
  "checkout_currency": "PHP",
  "use_promo": true,
  "promo_country": "PH",
  "promo_campaign": "plus-1-month-free"
}
```

## 常见结论

- `methods=link,card,cpmt_...` 且 `selected=cpmt_...`：这是新版 GCash，可继续主流程。
- `methods=card,link`：没有 GCash，换代理或扩大矩阵。
- `checkout/update HTTP 200` 但 `amount` 不是 0：优惠更新请求成功，但优惠未命中。
- `amount=None`：当前 OAICS 响应没有暴露金额，不等于优惠失败。
- 脚本退出码为 `2`：没有找到 GCash 支付方式；不是“优惠失败”的专用退出码。

不要把真实 token、cookie、代理密码或 JSONL 结果提交到 Git。
