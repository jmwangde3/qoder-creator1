# HANDOFF

交接说明。目标：换一个人接手时，不用重走这一路的坑。

- 仓库：`github.com/jmwangde3/qoder-creator1`（fork 自 `hirotomasato/qoder-creator`）
- 分支：`master`，本文写作时 HEAD = `a95a5ca`（滑块相关改动尚未提交）
- 单测：47 个，`python -m unittest discover -s tests` 全过
- 实测成功率：滑块改造后 **6/6 账号**（`n=3` + `n=1` + `n=3` 三段），
  滑块首次尝试通过 7/8（一次 F001 风控，重试即过）
- 已落库有效账号：76 个（在 `data/accounts.jsonl`，被 gitignore）

---

## 0. 滑块（改动最大的部分，先看这里）

### 0.1 落点靠闭环，不靠模型

拼图的位移不是鼠标位移的固定倍数：长拖约 1.02x，短修正约 1.8x（实测
`-21.2/254.3 +17.2/215.9`，21px 的光标步长让拼图走了 38px）。没有任何单一
gain 能同时解释两者，历史上据此写的 `÷gain` 和 offset 模型都以震荡收场。

现在的做法：

1. 主拖走到 `displayLeft * 0.92`，永远停在缺口近侧（拼图比光标跑得快，
   对准 100% 会过冲 3~9px，而控件不肯为 3px 的步长回退）；
2. `plan_trim()` 按剩余误差的 **35%** 逐步逼近，`steps=1`，每步加 ±0.8px 抖动；
3. 读数不动（<0.4px）就 `boost *= 2.2`，因为控件会丢弃 <1.5px 的移动；
4. 容差 **1.6px**，8 步上限。最小有效步长 1.5px × 1.8 = 2.7px，1px 容差
   物理上达不到，硬追只会在缺口两侧来回跳（曾跑满 10 步仍差 1.3px）。

### 0.2 F001 与落点无关

阿里云每次提交都回一个判定码，已挂 `-verify` 响应监听：

| 码 | 含义 | 处置 |
|---|---|---|
| `T001` | 通过 | — |
| `F001` | 风控（轨迹或环境） | 重试；连续 2 次换会话 |
| `F015` | 位置误差 | 重试（识别偏差） |
| `EXC` | 网络异常 | 重试 |

实测证据：`landed=198.7 / target=199.7`（残差 0.9px，全样本里最准的一档）
仍然判 **F001**。所以看到 F001 不要再去调落点，那是轨迹问题。

日志里对应 `Captcha: captcha verdict=F001 (flagged (trajectory or environment))`。

### 0.3 轨迹是下一个瓶颈

当前 `_smooth_move` 用 smoothstep（起手慢、中段最快、末端收），只是去掉了
"第一帧最快"这个最明显的生成器指纹。上游开源研究
（[slider-captcha-lab](https://github.com/Huan-zhaojun/slider-captcha-lab)，n=146）
给的真人形态是：

> 慢逼近（45~60% 帧只走 15~30% 距离）→ 谷底犹豫（3~6 帧 × 40~110ms）
> → 末端甩鞭（16~28 帧 × 2.5~6ms 走 65~75% 距离，峰值 5000~15000px/s）→ 收尾

他们把这条曲线复现后，首过率从 43.5%（旧平滑曲线）升到 90.9%；风控收紧时
旧曲线 0/9、新曲线 5/5。落点只偏正也是他们的结论（负偏差 F001 率 71% vs
正偏差 54%）。要再往上走，就照这个形态改 `_smooth_move`，但**先看 verdict
统计**：F001 < 10% 时改轨迹是负收益。

### 0.4 为什么"原版"看起来成功率更高

fork 的初版（`34f9f64`）拖动是 `delta = rem * track/max_pz`，`track` 取的是
**手柄宽度 40**（不是轨道长度），`max_pz = 300-52 = 248`，所以每步只吃掉剩余
误差的 16%。它主拖只走 16%，但后面有 **60 步**闭环，每步重读 `style.left` 再
算差值——慢，但纯负反馈、天然自适应，所以磨得到位。这一点已被 `plan_trim`
继承（只是把 16% 提到 35%、60 步压到 8 步）。

它真正的问题在别处：

- 识别：`ncc()` 的循环写成 `i < maskN`（应为 `i < mask.length`），`blur()`
  又把最外圈留 0，而 greedy alpha bbox 的最外圈正是轮廓。5 张人工量过真值的
  图，它 **0/5**，最大偏 159px。
- 判定：`_is_solved()` 在 wrapper 不存在时 `return True`，控件没渲染也算成功。

所以"原版成功率更高"多半是幸存者印象加上误判；可借鉴的只有闭环的形状。

---

## 1. 快速开始

```bash
cd H:\GIT\qoder-creator

# 依赖已装在 venv 里；换机器时：
python -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
./venv/Scripts/python.exe -m playwright install chromium

# 单测（纯逻辑，不需要网络）
./venv/Scripts/python.exe -m unittest discover -s tests

# 离线端到端（本地 mock 站点 + 真实 tempik 建箱，不碰 qoder.com）
./venv/Scripts/python.exe smoke_flow.py

# 真实跑
./venv/Scripts/python.exe main.py signup -n 10
./venv/Scripts/python.exe main.py view
```

`config.toml`（gitignored，仓库里有 `config.example.toml`）关键项：

| 键 | 当前值 | 说明 |
|---|---|---|
| `api.tempmail_base` | `https://014999.xyz/api` | **注意是裸域，不是 `tempik.` 子域** |
| `signup.headless` | `true` | CLI 也读这个值（以前写死 true） |
| `signup.concurrency` | `1` | 代码里没并发实现，改它无效 |

---

## 2. 环境坑（换机器必读）

这台机器到 Cloudflare 的链路有三层毛病，代码里都做了兜底，但**换机器前先测一遍**，否则会以为是代码坏了：

1. **死代理**：系统环境变量 `https_proxy=127.0.0.1:32664` 时通时不通。项目用 urllib/http.client，会无条件继承 → 跑之前 `env -u https_proxy -u HTTPS_PROXY ...`。
2. **IPv6 半通**：到 CF 的 IPv6 能建 TCP，但传输被 RST，Python 不会回退 IPv4。所以 `_KeepAlive` 强制走 A 记录直连 + SNI。
3. **丢包 33% / RTT ~300ms**：实测 `ping` 4/6 回包；新建连接 TLS 握手 median 1.7s、max 4.5s，成功率 3/8。**必须靠连接复用**——复用后 10/10，每次 0.33s。
4. **DNS 间歇性失败**：连续 5 次解析各卡 11 秒全部失败，第 6 次才成功。已做进程内缓存 + 落盘 `data/dns_cache.json` + 失败回退上次 IP。
5. **端口 8787 被占**：本机有 `workerd.exe`（tempik 的 `wrangler dev`）占着，mock 站点改用 **8799**。

版本：Python 3.13.14 / playwright 1.64.1 / chromium 156。

---

## 3. 代码地图

| 文件 | 职责 | 备注 |
|---|---|---|
| `main.py` | 菜单 + CLI + 批量循环 | 每个账号单独 try，一个失败不中断整批 |
| `src/config.py` | 读 `config.toml` | 有大量历史遗留常量，部分未被使用 |
| `src/tempmail.py` | tempik 客户端 + OTP 提取 | **最复杂的一块**：keep-alive、DNS 缓存、分层提取 |
| `src/signup.py` | 注册流程编排 | 密码步、滑块、OTP 轮次、PAT |
| `src/captcha.py` | 阿里云滑块求解 | 纯前端图像算法，成功率是外部条件 |
| `src/pat.py` | 建 PAT | 通过浏览器 fetch 带 cookie 发起 |
| `src/proxy.py` | 代理池解析 | 支持 http/https/socks5 |
| `src/stealth.py` | 浏览器启动参数 | Chromium + stealth context |
| `src/utils.py` | 日志、密码、JSONL | `generate_password` 有强约束，见下 |
| `tests/` | 29 个单测 | OTP 提取、代理解析、密码规则、批量韧性、DNS |
| `mock_qoder.py` | 本地假注册站（8799） | 复刻真实页面：自动提交、错误文案、401 重试 |
| `smoke_flow.py` | 离线端到端 | 会真实建一个 tempik 收件箱，其余全部本地 |
| `tools/inbox_trace.py` | 从 D1 读某地址的邮件 | 需要 `QODER_CF_ACCOUNT` / `QODER_D1_ID` |

---

## 4. 改代码前必须知道的 6 条约束

这 6 条都是踩出来的，破坏了就会退回原来的 bug：

1. **每次请求验证码都会让上一次的码失效**。所以 `wait_for_messages` 必须带 `since=本轮发送时刻`，只认那之后的邮件。否则会提交旧码 → 页面回 `invalid` → 白跑一轮。
2. **不能把「读不到」当成「没收到」**。收件箱读失败时 `wait_for_messages` 会延长等待，而不是返回空——返回空会触发重发，把已经到达的有效码作废。
3. **PAT 401 通常是「还没好」不是「永远不行」**。验证码通过后服务端要 2~3 秒才把会话标记为已验证，期间建 PAT 一律 401。已做 4 次 × 4s 重试。
4. **密码必须四类字符齐全**（长度 > 8、数字、大写、小写、特殊符号）。`generate_password` 现在按类构造，别改回纯随机——纯随机约 25% 不合格。
5. **一个账号失败不能拖垮整批**。`create_inbox` 在 `create_account` 的 try 里，批量循环也逐次 try。
6. **单连接复用是性能命脉**。`_KeepAlive` 不要改回每次新建连接，否则每轮 12 次握手，失败率立刻回到 1/3。

---

## 5. 本次会话修的 16 个提交

```
a95a5ca  OTP 放弃时记录收件箱里到底有没有信
685229b  密码按规则构造；共享 tempik 客户端；超时 20s→12s
dce303c  建箱失败不再拖垮整批；DNS 缓存 + 回退
8591627  OTP 判定收紧，PAT 401 重试
9dff01b  连接复用（握手 10 次 → 1 次）
2fca005  只认本轮发送之后的邮件，杜绝提交旧码
be261a2  密码步必须确认真的前进
49336c8  代理解析支持 socks5
bf47ef2  滑块控件不渲染时不再空等 75 秒
3262f84  补测试：欢迎邮件不能顶掉验证码
abbd035  返回按钮按文字匹配（真实页面是 `‹ Go back`，不是 button）
02647bc  没有重发按钮时，用「返回 → 重填密码 → 再过滑块」触发新码
e868994  OTP 提取分层，不再抓第一个 6 位数
f7b60d4  所有 API 调用加重试
82304a3  域名获取重试、点击只看可见节点、CLI 读 headless 配置
577811c  删掉误提交的 CF 调试 JSON、修 README 编码
```

---

## 6. 失败画像（实测，9/10 那轮）

| 类型 | 频率 | 能否修 | 判断依据 |
|---|---|---|---|
| qoder 不投信 | ~1/10 | **不能**，服务端行为 | D1 里该地址 0 封邮件，而三次发送都成功提交了 |
| 滑块求解失败 | ~1/10 | 不能根治，只能重开号 | `Captcha state: {...}` 全 false |
| 网络超时 | ~1/10 | 已压到最低 | `read timeout` / `handshake timeout` |
| 密码不合规 | 已修 | — | 页面 `form says: The password must be...` |
| 提交旧码 | 已修 | — | 页面 `invalid or incorrect` |

**剩余失败基本不是代码能救的**，因此下一步的收益点在「别浪费失败掉的号」，而不是继续抠成功率。

---

## 7. 排查手段

```bash
# 一条命令看某个账号收到了什么（需要 D1 凭据）
QODER_CF_ACCOUNT=... QODER_D1_ID=... python tools/inbox_trace.py someone@014999.xyz

# 直接查库
cd H:\GIT\tempik
wrangler d1 execute tempik-db --remote --command \
  "SELECT inbox_address, subject, received_at FROM messages ORDER BY received_at DESC LIMIT 10;"

# 实时看 worker 日志（确认邮件有没有进 worker）
wrangler tail tempik --format pretty
```

日志里几个关键行：

```
Resend: fresh code requested at ...        ← 这一轮的 since 基准
N mail(s) ... were older than since=...    ← 被判为过旧、没提交的邮件
form says: ...                             ← 表单拒绝的真实原因
OTP ... rejected by the verification screen
PAT answered 401 (attempt 1/4)             ← 会话滞后，正常重试
OTP failed for ...; inbox holds 0 message(s) — nothing was sent
```

失败截图在 `data/failure_<n>_<step>.png`，step 可能是 `password` / `captcha` / `otp_rejected` / `otp` / `pat`。

---

## 8. 没做的事 / 建议的下一步

1. **失败队列**（价值最高）：现在 `-n 10` 失败 1 个就是少 1 个，重跑从 0 开始。建议加 `data/retry.jsonl` + `--retry-failed`，跑完补足目标数量。
2. **断点续跑**：同上，需要一份 state 文件记录已完成的序号。
3. **轮询参数外置**：`OTP_WAIT_SECONDS=60`、`interval=5`、`OTP_ROUNDS=3` 都是常量，可以挪进 `config.toml`。
4. **`except ...: pass` 收窄**：全项目 10 处（`signup.py` 5、`tempmail.py` 2、`captcha.py` 1、`pat.py` 1、`utils.py` 1），目前都配了日志，但值得逐个确认。
5. **tempik 侧**（不在本仓库）：`H:\GIT\tempik\wrangler.toml` 里的 `[email]` 字段是无效配置，wrangler 会报 `Unexpected fields: email`，可以直接删掉，不影响功能。

---

## 9. 仓库之外的东西

- `H:\GIT\oc-ref`：另一个项目 `okky-x0f/qoder-creator` 的克隆，**与本仓库无关**，只是当时做过对比，可以删。
- `H:\GIT\tempik`：临时邮箱服务（Cloudflare Worker + D1），本项目的邮箱依赖它。
- `data/accounts.jsonl.bak`：删脏记录前的备份。
- `data/` 全部被 gitignore，含真实账号密码与 PAT，**不要提交**。
