# HANDOFF

交接说明。目标：换一个人接手时，不用重走这一路的坑。

- 仓库：`github.com/jmwangde3/qoder-creator1`（fork 自 `hirotomasato/qoder-creator`）
- 分支：`master`。滑块改造从 `c69906f` 起，后续提交见 `git log -1`
  （本文不追具体 hash，避免每次提交都过期）
- 单测：64 个，`python -m unittest discover -s tests` 全过（含限时人工兜底、
  选择器回退的 Chromium 集成测试，见 §0.8）
- 实测成功率：滑块闭环重写后累计 **26/26 账号**；2026-11-05 11:30 那轮
  **滑块 9/9 会话 T001（100% 首试）但账号 0/3**，卡在 tempik 邮箱接口超时，
  不是滑块问题，详见 §0.9-B
- 已落库有效账号：104 个（在 `data/accounts.jsonl`，被 gitignore）

**姊妹会话**：`H:\AI工作区\本机软件相关AI工作区\qoder-creator`（OAuth 写回链路，
用本仓库的账号跑登录+额度写回，滑块代码是本文件的移植版，见 §0.6）。

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
3. 读数不动（<0.4px）说明控件把这步整个丢掉了：下一轮改从
   `applied_cursor`（**产生当前读数的那次光标位置**）按 1:1 直接补差。
   这是姊妹会话修的一个真 bug——丢步后 `cursor_x` 已经前进，若还拿它
   当基准，等于把丢掉的行程算两遍；
4. 容差 **1.6px**，12 步上限。最小有效步长 1.5px × 1.8 = 2.7px，1px 容差
   物理上达不到，硬追只会在缺口两侧来回跳（曾跑满 10 步仍差 1.3px）。
   12 而不是 8 是因为 1:1 的本地 mock 每轮只关 65% 的差距，8 轮会差 ~2px；
   真实控件每轮 1.8x，3~5 轮就完事。

短步增益的实测锚点（不要再用"大概 1.0"）：

| 光标步长 | 拼图实际走 | 倍率 |
|---|---|---|
| 7.6px | 14.4px | 1.89 |
| 21px | 38px | 1.81 |
| 27.2px | 48.8px | 1.79 |
| 200px 主拖 | ~204px | 1.02 |

### 0.2 F001 与落点无关

阿里云每次提交都回一个判定码，已挂 `-verify` 响应监听：

| 码 | 含义 | 处置 |
|---|---|---|
| `T001` | 通过 | — |
| `F001` | 风控（轨迹或环境） | 重试；连续 2 次换会话 |
| `F015` | 位置误差 | 重试（识别偏差） |
| `EXC` | 网络异常 | 重试 |

实测证据（三次 F001，落点全部无懈可击）：

| target | landed | 残差 | 判定 |
|---|---|---|---|
| 199.7 | 198.7 | +0.9px | F001 |
| 241.2 | 241.1 | +0.1px | F001 |
| 185.5 | 187.1 | −1.6px | F001 |

看到 F001 不要再去调落点，那是轨迹或环境问题。重试即过的概率很高
（10 次会话里唯一一次 F001，第 2 次尝试就 T001）。

一个**已初步坐实的线索**（42 次有判定码的真实拖拽）：

| target | 次数 | F001 | F001 率 |
|---|---|---|---|
| > 240 | 7 | 2 | **29%** |
| ≤ 240 | 35 | 2 | **6%** |

姊妹会话的独立链路上，两次 F001 也都在 244.9 / 249.1。两条独立链路指向
同一件事：**大位移更容易被风控**。物理上说得通——行程长，要么事件更多、
要么峰值速度更快，轨迹暴露面更大。样本仍小（n=7 vs n=35），先记录；
若后续 F001 率继续这样分布，下一步是对 target>240 的挑战改用「中途
犹豫」的两段式主拖（模拟人在长行程中段的停顿），而不是继续抠落点。

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

**但要修正一个早期的过度判断**：上面那句"0/5"是在 5 张**全部是白化缺口**
的人工标注样本上测的。真实流量里缺口形态是混合的（暗化/白化都有），
A/B 实测原版在 57% 的会话里一次就对（见 §0.5）。原版不是"不能用"，
是**在白化缺口上会瞎**，那类图大约占 1/5~1/4。

### 0.5 A/B 实测：原版 vs 现在（各 5 个账号，2026-11-05 02:40）

用 `QODER_CAPTCHA_IMPL=legacy` 环境变量把 `34f9f64` 的求解器原样挂进
当前 signup 流程（`src/captcha_legacy.py`，一处没改），同一台机器、同一
条网络、同一套 OTP/PAT 逻辑，只换滑块实现。统计工具：
`python tools/captcha_ab.py <标签> <起始行号>`，按"滑块会话"（从
`attempt 1/5` 到 `solved`）计数——按账号计数会把 OTP 重发误算成滑块失败。

| 账号 | 原版（尝试次数） | 当前（尝试次数） |
|---|---|---|
| 1 | 1 | 1（另一会话 2，F001 重试） |
| 2 | 1 | 1、1 |
| 3 | 3 | 1、1 |
| 4 | 1、1 | 1、1、1 |
| 5 | 3、3 | 1 |
| **合计** | 13 次 / 7 会话 | **11 次 / 10 会话** |
| **平均** | 1.86 次 | **1.10 次** |
| **一次过率** | 4/7（57%） | **9/10（90%）** |

两组账号都是 5/5 创建成功。结论：**最终成功率两者都能到 100%（在小样本
下），差距在效率**——每次滑块验证平均少走 0.76 次尝试，也就是每个账号
少花约 8~10 秒和一次风控暴露。样本量（7 vs 10 会话）做 Fisher 精确检验
p≈0.26，统计上还不显著，方向一致。

复跑：`data/ab_legacy_mark.txt` / `data/ab_current_mark.txt` 存了两组的
日志起始行号。

### 0.6 给姊妹会话（OAuth 写回）的读者

你在 `H:\AI工作区\本机软件相关AI工作区\qoder-creator` 用本仓库的账号跑
登录+额度写回，滑块是同一条阿里云链路。以下是对你直接有用的东西：

1. **先看判定码，再动手**。`_install_verdict_hook()` 拦 `-verify` 响应，
   `T001` 通过 / `F001` 风控 / `F015` 位置误差 / `EXC` 网络。看到 `F001`
   不要碰落点参数——三次实测落点 ≤1.6px 仍被判 F001。连续 2 次 F001
   换环境，别硬磕（上游研究的结论）。
2. **落点增益不唯一**：短步 1.8x、主拖 1.02x（§0.1 的表）。`gain=0.16`
   的老谜题答案：那是手柄宽 40 ÷ 拼图行程 248，不是物理增益；把它当
   除数会把光标推到轨道尽头（260 = 300−40，就是这么来的）。
3. **读数要用 inline style**，`getComputedStyle().transform` 是 transition
   的插值，拖动中读到的一定落后。`_settled_x()` 要求 3 次相同 + ≥250ms，
   别改回两次相同。
4. **不要用 1px 容差**：最小有效步长 1.5px × 1.8 = 2.7px，1px 物理不可达，
   闭环会跑满所有轮数在缺口两侧来回跳（这条是踩出来的，别再走一遍）。
5. `plan_trim()` 是纯函数（不碰浏览器），`tests/test_captcha_drag.py` 里
   有收敛仿真（gain 1.0~1.1 × 1.0~1.8 的组合矩阵）。改拖动逻辑先跑它。
6. 你的 `captcha_solver.py` 已带同一套参数（TRIM_DAMPING=0.35 等）。
   两个仓库要保持同参：本仓库改动走 `src/captcha.py`，移植时对照
   `git log --oneline -5 -- src/captcha.py` 取最新。
7. mock（`tests/manual_captcha_widget.py`）是 1:1 映射且 transform 驱动，
   **不代表真实控件**：真实是 left 驱动 + 1.8x 短步 + 丢步行为。mock 5/5
   只证明"没退步"，不证明"真实能过"——必须配合真实 signup 的 verdict 统计。

### 0.7 外部项目 `jmwangde3/qoder-autopilot` 调研（2026-11-05）

本地克隆 `H:\temp\user\qoder-autopilot`，HEAD `299109f`，PyPI `qoder-autopilot 0.6.3`
（README 里的 clone 地址仍写 `Daivageralda/qoder-autopilot`）。同一条 Qoder 注册
链路，但**滑块比我们弱一档**：`captcha/slider.py` 一次性拖到底 + 过冲回拉，没有
读数、没有闭环、没有判定码；`solver.py` 只判断控件是否消失；`tests/` 里 captcha
零测试；最新提交把默认改成人工滑块（README 也写 manual "most reliable"）。
所以它的价值在工程化和 OAuth/9Router 集成，不在识别/拖动算法。

**离线实测**：拿我们 7 组人工真值 fixture（3 darken + 3 white + 1 no_hole），
用 `G:/Python313/python.exe`（cv2 4.14）复现他们的 4 方法：

| 复现方式 | 命中 ±25px | 说明 |
|---|---|---|
| 原样 4 方法投票 | **0/7** | 真实 puzzle 有两种格式，`480×240` 全画布 sprite / `52×200` 小图。他们没做 alpha bbox 裁剪，模板与背景同宽 → matchTemplate 只剩 1 列，亮度/边缘的搜索循环空转，全部返回图中央 240 |
| 先按我们的方式裁 alpha bbox | **2/7** | 3 个白化缺口的 NCC 全偏 −55~−57px，分数却高达 0.982~0.995；投票把正确答案带偏（photo_c 真值 131、NCC 131 → 投票 72） |
| 单用 masked NCC | 4/7 | 3 个 darken 全对，3 个 white 全错 |

两条硬结论：

1. **多方法投票别抄**。`ccoef` 与 `sqdif` 同源、永远同时对错，「一致票数」恒为 2，
   是伪多样性；「列亮度下降」「边缘密度最大」噪声太大，会盖过正确的 NCC。
   我们已证明绝对 score 不能作判据（§0.2），这次连「通道一致性」也不成立。
2. 再次印证白化缺口是**半透明白蒙版**而非暗影：NCC 0.995 却偏 55px。
   他们的 `ai_vision.py` prompt 写死 "dark shadow / darker area"，照抄必翻车。

**值得吸收的（按优先级）**

1. **末端过冲 + 回拉**（`slider.py:80-85`）：先多走 3~10px 再回 target。我们的
   闭环是纯单调逼近、从不过冲；§0.3 说下一个瓶颈是轨迹，F001 又在 target>240
   富集。这是唯一能直接 A/B 的形态改动。注意按短步 1.8x 换算、在 `mouse.up()`
   前完成，用 verdict 统计判定，**别拿落点当指标**。
2. **多选择器回退 + `offsetParent !== null` 可见性判断**（`solver.py:28-50`、
   `slider.py:27-40`）：我们全是硬编码 `#aliyunCaptcha-*`，站点换版会全挂。
   低成本、纯健壮性。
3. **人工兜底模式**（`manual.py`）：连续 N 次自动失败时开可见窗口让人拖一次，
   轮询控件消失后自动继续。我们现在失败就弃号，F001 连续 2 次尤其适合。
4. **AI Vision 只做兜底**（`ai_vision.py`）：本地双通道都不确定时才调 API。预处理
   可抄（按拼图 y 裁 strip、CLAHE、Canny、原图三联拼接、alpha 轮廓红边），
   但 prompt 必须改成「亮或暗的异常区域」。
5. **Camoufox 反检测**（`browser/camoufox.py`）：C++ 级指纹，理论上能压 F001。
   风险：Firefox 内核（我们的 mouse 事件 / 1.8x 增益 / left 读数都在 Chromium
   上标定）、`pyproject.toml` classifiers 只列 macOS/POSIX、要额外下载浏览器。
   只做小样本 A/B，别直接换。
6. **工程化**：`doctor` 健康检查（正好治我们踩过的「默认 python 3.13 没有
   playwright」环境坑）、Pydantic Settings、`--dry-run` / `--format json|csv` /
   `--log-file`、并行 staggered 2s + `asyncio.gather(return_exceptions=True)`、
   异常层级、截图自动清理。我们 `main.py` 目前只有 `signup` / `view`。
7. **OTP 的 CSS 颜色黑名单**（`auth/otp.py`）：`000000`/`ffffff` 等 18 个 6 位
   十六进制会被误当验证码；letter-spacing / 大字号两级优先也可抄。

**给姊妹会话（OAuth 写回）的对照**：`auth/oauth.py` 有完整 PKCE device flow
（challenge=S256、nonce、machine_id、verifier → `deviceToken` 轮询，202/404
pending → 200 token，`User-Agent: Go-http-client/2.0`）；`infra/ninerouter.py` 有
`providerConnections` 表结构与 data JSON 字段（expires_in 封顶 30 天、WAL +
busy_timeout）；`infra/relay.py` 是带 Bearer token + 30req/60s 限流的 FastAPI
远程写库服务 —— 如果桌面端 Job 沙箱写不到本机 DB，这是现成方案。

**别抄**：`solver.py:136-143` 的 retry offset（首试 ±2px、2~3 次 ±5px、之后
±15px 随机）。F001 与落点无关，给同一个 target 加噪声只会把好落点弄坏；重试
应该换图重发，我们已经有这条链路。一次性拖动同理。

### 0.8 本轮落地：限时人工兜底 / 选择器回退 / doctor（2026-11-05 11:20）

从 §0.7 里挑了低风险的三条做进代码，**轨迹和浏览器内核一律没动**。

**① 限时人工兜底**（`src/captcha.py::_wait_for_human`）

自动 5 次全失败后，如果浏览器可见，给人一次拖的机会；**默认 45 秒硬上限**，
到点自动放弃并回到原来的失败链路，绝不挂住批量。和对方的 `manual.py` 有
三点关键差别：

| | 对方 | 我们 |
|---|---|---|
| 超时 | 120s，且只在 manual 模式启用 | 45s 可配，自动失败后的兜底 |
| headless | 强制非 headless | **headless 直接跳过**（没窗口可看，等也白等） |
| 成功判定 | 控件消失即成功 | `T001` 判定码，或连续 2 次「控件不可见」 |

最后一条防的是假成功：控件被拒后会重载，重载那一瞬间 DOM 里同样找不到它，
只读一次会误判成"人已经拖过去了"。

配置在 `config.toml`：

```toml
[captcha]
manual_fallback = true
manual_timeout = 45
```

环境变量 `QODER_MANUAL_FALLBACK=0` / `QODER_MANUAL_TIMEOUT=10` 可覆盖，
无人值守的定时任务建议显式关掉。入口：
`./venv/Scripts/python.exe main.py signup -n 3 --headed`
（交互菜单里对 "Headless mode?" 答 no 等效）。

**默认 `headless = true` 的批量永远不会触发它，这是刻意的**：没人盯屏幕时
等 45 秒纯属浪费。日志里出现
`captcha manual fallback skipped (headless or disabled)` 就是跳过。
每账号独立计时，单次会话最多 45s。

**② 多选择器回退 + 可见性判断**

`_WIDGET_FIND_JS` 定义 img / puzzle / slider / wrapper 四组候选，canonical ID
优先，找不到才用 `.nc-container`、`.slide-verify-slider` 等通用形状，一律用
`offsetParent !== null` 判可见。`widget_state` / `is_solved` / `READY_JS` /
点击触发 / `wait_for_selector` 都改成这套。

**边界**：`SNAP_JS` / `SOLVER_JS` / `PUZZLE_X_JS` 仍用 canonical ID —— 站点
真换版时图像识别和坐标换算要一起重写，只换选择器会把错误目标喂给闭环。
`tests/test_captcha_selectors.py` 用真实 Chromium 渲染两套 DOM 验证。

**③ OTP 的 CSS 颜色黑名单**

`<style>` 内容不会被 `_plain()` 的标签剥离删掉，`color:#000000` 会留下一个裸
`000000`：它既能抢关键词匹配，也会让「唯一 6 位数」变成两个而判模糊。现在
18 个颜色码在关键词匹配中跳过、在唯一性判定中剔除。

**④ `doctor` 命令**

`python main.py doctor` 检查 Python 版本、当前解释器有没有 playwright、
Chromium 二进制、headless/兜底配置、temp-mail 连通性、data 目录可写。针对的
就是 §2 里「默认 `python` 是 WorkBuddy 3.13 没装 playwright」这个坑。

**⑤ 测试日志隔离（本轮踩到的坑）**

`write_log()` 原来无条件写 `data/qoder.log`，而单测会跑真实的 signup/captcha
代码。本轮 A/B 验证进行中重跑了一次单测，日志里凭空多出
`[2] Unhandled signup error: boom` 和 4 条 `captcha attempt 1/2` ——
`tools/captcha_ab.py` 会把它们算成真实滑块会话。

两处修复：`write_log()` 支持 `QODER_LOG_FILE` 覆盖，并在 unittest 进程里自动
重定向到系统临时目录（注意 `discover -s tests` **不会**导入
`tests/__init__.py`，所以在那里设环境变量是无效的，必须在函数里判断）；
`captcha_ab.py` 只统计 `captcha attempt n/5`（真实 signup 的 max_attempts，
测试与本地 mock 是 2/3）。复跑旧日志时这个过滤也能挡住残留脏行。

**单测**：64 个全过。新增 `tests/test_manual_fallback.py`（限时、T001、
重载闪烁不假成功、连续两次才算、接线与 headless 不等待）、
`tests/test_captcha_selectors.py`（两套 DOM）、OTP 三条用例。

**没动的**：末端过冲回拉需要真实 A/B 才能证明不是负收益；Camoufox 换 Firefox
内核风险太大；AI Vision 需要外部 API key；多方法投票实测 2/7，明确不采用。
对方的并行注册和 `--format json|csv` 也没搬：并行要改 TempikClient 的 session
共享模型（现在整批共用一个邮箱会话），风险远大于收益，而输出格式目前
`view` 命令已经够用。

### 0.9 交接清单（2026-11-05 11:50，给下一个 agent）

**一句话状态**：本轮该改的都已改完、单测 64/64、mock 5/5；真实链路上滑块
**9 个会话 9 次 T001、全部首试通过**；但 3 个账号仍然 0 成功，卡在邮箱接口，
不是滑块。下一步先修邮箱可用性，再考虑轨迹。

#### A. 本轮已完成，不要重复做

| 改动 | 位置 | 状态 |
|---|---|---|
| 限时人工兜底（45s 可配，headless 跳过，T001/连续两次消失才算成功） | `src/captcha.py::_wait_for_human`、`solve_slider_local` | 单测覆盖，真实 headed 未验收 |
| 多选择器回退 + 可见性判断 | `_WIDGET_FIND_JS`、`is_solved`、`widget_state`、`READY_JS` | Chromium 集成测试覆盖 |
| OTP 的 CSS 颜色黑名单 | `src/tempmail.py` | 3 条新用例 |
| `doctor` 预检 | `main.py::doctor` | 实跑通过（5 ok / 1 warn） |
| `--headed` 入口 | `main.py` | `signup -n 0 --headed` 验证过提示 |
| 测试日志不再污染 `data/qoder.log` | `src/utils.py::write_log` | 已验证 boom 条数不增长 |
| `captcha_ab.py` 只统计 `attempt n/5` | `tools/captcha_ab.py` | 历史区间回归过 |

配置在 `config.toml` 的 `[captcha]`：`manual_fallback = true`、
`manual_timeout = 45`；环境变量 `QODER_MANUAL_FALLBACK` / `QODER_MANUAL_TIMEOUT`
可覆盖。

#### B. 当前唯一的阻塞：tempik 邮箱接口（P0）

本轮 `signup -n 3` 的完整结果：滑块 9/9 T001（每个账号 3 个会话：首试 +
两次 OTP 重发），但账号 0/3，日志全是：

```
Tempik GET /session attempt 2/5 failed: The read operation timed out
Tempik GET /inboxes/<addr>/messages attempt 1..3/5 failed: read operation timed out
[3] OTP failed for ...; inbox holds 0 message(s) — nothing was sent
```

刚测的现状（11:49，直连不走代理）：

| 端点 | 结果 |
|---|---|
| DNS `014999.xyz` | 正常，104.21.82.183（Cloudflare） |
| `GET /api/session` | **200，1.96s**（服务活着） |
| `GET /api/config` | **12s 超时**（http=000） |
| `GET /inboxes/{addr}/messages` | 注册时 12s 超时 ×3 |

所以不是宕机，是**慢**：`_REQUEST_TIMEOUT = 12.0` 对当前延迟不够，5 次重试
每轮要空转 ~60s，`OTP_ROUNDS=3` 会把一个账号拖到 3 分钟以上，最后仍然
`inbox holds 0 message(s)`。

**不要用改滑块的方式"修"这个**。滑块这段时间的表现是 100% 首过。

下一步按这个顺序：

1. 先量化 `/messages` 的真实延迟，别猜。用一个已知邮箱跑：
   ```bash
   env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY \
     curl -s -m 30 -w '\nhttp=%{http_code} time=%{time_total}s\n' \
     "https://014999.xyz/api/inboxes/<address>/messages"
   ```
   连续 5 次，记 p50/p95。
2. 如果 p95 > 12s：把 **读请求**（`get_messages` / `wait_for_messages`）的
   超时单独提到 25~30s，重试降到 3 次；写请求（`create_inbox`）保持 12s。
   在 `src/tempmail.py` 里 `_REQUEST_TIMEOUT` 是一个常量，注意别把注册表单
   提交也拖慢。
3. 如果 p95 正常但 `/messages` 仍超时：考虑第二 provider（对方项目的
   `temp_mail.py` 有 `TempMailProvider` ABC + Cloudflare/Moca 两家，可以照
   那个形状加一个 fallback，但**先测量再动手**）。
4. 修完用 `signup -n 2` 验收：至少 1 个 `PAT valid=True`，且
   `captcha_ab.py` 仍显示 100% T001。验收通过前不要动轨迹。

#### C. 过冲回拉 A/B（P1，目前唯一还能提首过率的改动）

现状：44 次有判定码的真实拖拽里 F001 5 次（11%），且 `target>240` 时 29%
（2/7）vs `≤240` 6%（2/35）。对方 `slider.py:80-85` 的形态是：主拖结束后
先多走 3~10px，再回到 target，然后 `mouse.up()`。

移植时的三个硬约束：

- 短步增益是 **1.8x**：3~10px 的光标过冲会让拼图走 5.4~18px，可能直接跨过
  缺口。过冲量必须按 `landed` 读数换算，不能盲走。
- 过冲必须发生在 `mouse.up()` **之前**，且最后要回到 `target`，否则控件
  按最终位置判分。
- 加一个常量开关（建议 `OVERSHOOT_PX = 0.0`，0 表示关闭），默认关闭；
  A/B 时用环境变量或改常量，别直接改死。

验收标准：**≥20 个真实会话**，F001 率显著低于基线 11%，`|residual|` 仍
≤1.6px，且不出现 F015（位置误差）。样本不够时不要下结论——现在 F001 只有
5 个，噪声比信号大。若 F001 率没有下降，把常量设回 0 即可回滚，无其他副作用。

#### D. 真实 headed 人工兜底验收（P1，需要人在场）

单测覆盖了逻辑，但**真实路径一次都没跑过**（默认 headless 全部跳过）。
需要一个人坐在电脑前：

```bash
QODER_MANUAL_TIMEOUT=20 ./venv/Scripts/python.exe main.py signup -n 1 --headed
```

- 看到黄色面板后手动把拼图拖对 → 期望日志
  `captcha solved manually (T001) after Ns`，账号继续走 OTP。
- 故意不拖 → 期望 20s 后 `captcha manual fallback timed out ... giving up`，
  进程继续下一个账号（若 `-n > 1`），不挂起。
- 两条分支都出现过才算验收通过。

#### E. 可选、但别现在做

- **Camoufox**（P2）：唯一可能直接压 F001 的手段，但要换 Firefox 内核，
  我们的 `left` 读数、1.8x 短步增益、mouse 事件时序全是在 Chromium 上标定的。
  除非 F001 升到 20%+，否则不值得。真要做，先用 `tools/captcha_ab.py` 跑
  同一批 challenge 的小样本对照，别直接替换 `launch_stealth_browser`。
- **AI Vision 兜底**（P3）：只在双通道都不确定时调，需要 OpenAI 兼容 key。
  预处理可抄对方（按拼图 y 裁 strip、CLAHE、Canny、三联图），但 prompt 必须
  写「亮或暗的异常区域」——白化缺口是半透明白蒙版，抄 "dark shadow" 必错。
- **并行注册**：不要。`TempikClient` 整批共用一个 session，并行会互相踩。

#### F. 复跑命令（照抄即可）

```bash
cd /h/GIT/qoder-creator

# 1. 预检（当前解释器有没有 playwright / Chromium / 配置）
./venv/Scripts/python.exe main.py doctor

# 2. 单测（64 个，约 3s；会写临时日志，不污染 data/qoder.log）
./venv/Scripts/python.exe -m unittest discover -s tests

# 3. 本地 mock 控件（5 轮，验证拖动闭环没退步）
./venv/Scripts/python.exe tests/manual_captcha_widget.py 5

# 4. 真实注册（必须去掉代理变量，否则走代理会 502）
wc -l < data/qoder.log > data/mark.txt
env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY \
    -u all_proxy -u ALL_PROXY \
    ./venv/Scripts/python.exe main.py signup -n 3

# 5. 按滑块会话统计（不是按账号：OTP 重发会再过一次滑块）
./venv/Scripts/python.exe tools/captcha_ab.py "label" $(cat data/mark.txt)
```

注意：**默认 `python` 是 WorkBuddy 的 3.13，没有 playwright**，必须用
`./venv/Scripts/python.exe`。

#### G. 两个仓库状态，接手者需要决定

1. `HANDOFF_AGENT_SIDE_20261105.md` 在工作区显示为 `deleted`（未提交）。
   不是本轮删的，也没有擅自恢复。要么 `git checkout -- <file>` 恢复，要么
   在下次提交里确认删除。
2. `data/` 被 gitignore：账号 104 个；本轮 3 个失败账号的邮箱已经消耗掉，
   重跑会新建邮箱，不影响。

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

> 最新优先级和可执行步骤见 **§0.9**（2026-11-05 交接清单）。本节是更早的
> 长期 backlog，仍然有效，但排在 §0.9 之后。

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
