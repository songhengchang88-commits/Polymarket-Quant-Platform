# 实盘就绪审计与缺陷修复记忆 (2026-11-15)

> 本文件是 `Polymarket-Quant-Platform` 的版本化记忆。断线重连后先读本文件，
> 即可恢复"已修什么、还剩什么阻塞、下一步打哪"的完整上下文。

---

## 一、系统真实形态（调研结论）

**这不是一个已能实盘下单的交易系统，而是一套"纸面/研究态"量化平台。**
证据：

1. `pmq/execution/atomic.py` 的 `AtomicPairExecutor` 是全仓库唯一真正调用
   `post_order` 的类，但 **全仓库零实例化**（只有 `tests/test_atomic.py` 用它）。
2. `sdk_adapter.py:11` 自述："心跳（订单死人开关）本步不实现，仅预留 HeartbeatPort 接口位置"；
   第 345 行 `_not_enabled("订单心跳发送", "步 5")` —— 心跳发送是占位空实现。
3. `pmq/cli.py` 的 `--live` 分支（`cmd_probe`）自述"live 模式尚未在本机验证过"。
4. 账户侧核查：链上 `USDC.e 余额 = $0.00`，`CTF Exchange 授权额度 = $0.00`。

**结论：实盘缺口在"执行链路未接线 + 账户无资金无授权"，不在策略代码。**

### 策略可行性实证
- `pmq.cli scan --limit 40` → **通过全部闸门 0/40（0.00%）**。
  拒绝分布：配对成本 36 / 极端价位 16 / 价差 10 / 盘口缺失 4。
- 与 `docs/GITHUB_RESEARCH.md` 记载的真实基线（0/40）一致 —— 挂单吃单的"
  二元平价套利"在当前市场结构下**根本不存在**，自研结论正确，方向应转向**做市**。
- 5m 加密做市的纸面账本：近两个 2 小时窗口分别为 **-8.37U** 与 **+2.25U**，
  BTC 连续两窗口为最差品种（-5.99U / -2.81U），SOL 在第二窗口 +4.27U。

---

## 二、本轮已修缺陷（全部有测试覆盖）

| # | 位置 | 缺陷 | 修法与影响 |
|---|------|------|-----------|
| 1 | `pmq/risk.py` `max_shares_for_budget` | 强加"maker amount 必须 2 位小数（美分）"的**伪硬约束**，把 3.06 份砍成整数 3 份，白丢最多约 6% 部署本金 | 对齐官方 `ROUNDING_CONFIG`（tick 0.01 → amount 4 位小数，实测 `1.4994 USDC` 合法）；改为按 tick 取 amount 位数上限的可表示性校验，正常对齐下永不触发 |
| 2 | `pmq/risk.py` 同上 | `leg_1 = round(shares*price_1, 2)` 下一行被 `leg_1 = shares*price_1` 覆盖（死赋值），单腿红线两腿口径不一致 | 两腿统一按**未取整真实支出**判定（更保守），去掉死赋值 |
| 3 | `pmq/execution/atomic.py` `_unwind` | **最严重**：只要有任何一份成交就返回 `True`，调用方随即把 `unhedged_shares` 写成 `0.0` —— FAK 部分平仓时的**残余裸敞口被静默抹掉**，账面上"已补偿"、实际裸露在市场里 | 改为返回**实际平掉份额**；调用方用 `residual = max(0, 目标 − 已平)` 判定状态，残余 >0 一律 `UNHEDGED` 并写明"平仓不彻底" |
| 4 | `pmq/execution/atomic.py` `_unwind` | `round(shares, 2)` 会**向上**进位，可能卖出超过实际持有份数（拒单/卖空）；且每次重试都拿原始份数去卖，不扣已成交部分 | 改为向下取整 `floor(x*100)/100`；重试循环维护 `remaining`，逐次递减 |
| 5 | `pmq/execution/atomic.py` `make_order_args` | size 取 4 位小数，与官方 `round_down(size, 2)` 不一致 → 本地账本份数与真实成交份数错位 | 本地同样向下对齐到 2 位小数；amount 兜底改为按 tick 的真实位数上限 |
| 6 | **环境级（最阻塞实盘）** | 进程级 `NO_PROXY` 含 `[::1]`，httpx 转成挂载键 `all://[::1]` → `URLPattern` 端口解析抛 `InvalidURL: Invalid port: ':1]'` → **`httpx.Client()` 一构造就崩**，导致 `py_clob_client` V1 与 V2 的 `http_helpers`（模块级 `httpx.Client(http2=True)`）全部 import 失败，**CLOB 凭证派生直接不可用** | 新增 `pmq/netclean.py`，在 `pmq/__init__.py` 最先执行清洗；`tools/check_wallet_readiness.py` 同步接入。**修复后 EIP-712 凭证派生握手实测成功（1518ms，派生出 API Key）** |
| 7 | `pmq/execution/deadman.py` | 任何一次**正常** `stop()` 都被 atexit 当成进程死亡，刷误导性 `PROCESS_ATEXIT` 紧急撤单告警；且 `stop()` 只标"优雅"却**根本没撤单**（裸奔停服） | 区分异常/优雅两种停机语义：两者**都撤单**，但优雅路径不置事故位、不写 CRITICAL、atexit 不再误报；撤单失败自动升级为紧急路径 |
| 8 | `pmq/execution/deadman.py` | 全局注册表**只增不减**，长期运行累积失效实例；`shutdown_all` 持锁调用 `stop()` 会与内部注册表加锁形成**非重入死锁** | 触发/优雅停后自我摘除；`shutdown_all` 先在锁内快照、释放锁后再逐个停机 |
| 9 | `pmq/execution/atomic.py` | 死人开关完全没接进执行链路（`DeadManSwitch` 全仓库零引用） | 执行器新增可选 `deadman` 注入，双腿成交后喂狗；新增 `stop()` 供 finally 调用的优雅停机 |
| 10 | `test_system.py` | **假绿测试**：try/except 吞掉所有异常且无任何断言，服务没起来也 PASS | 改为诚实的集成测试：端点不可达 → `pytest.skip`；可达 → 对返回结构强断言 |
| 11 | `tests/` 12 个测试文件 | 历史提交把 11 个测试文件删除（"与做市栈零交集"），造成覆盖率回退 | 从 git 历史按 `cmd /c` 字节保真恢复（PowerShell 重定向会写成 UTF-16 并引入 NUL 字节导致 `SyntaxError`）。**恢复过程中暴露并修掉了上表 #1/#2/#3/#4 四个真实缺陷** |
| 12 | `pmq/risk.py` `Limits` | **neg-risk 事件组集中度上限完全缺失**：`neg_risk` 只是 book dict 的透传字段，`check_pair` 只看总额敞口。同一 `condition_id` 下的多个 outcome 按构造相关 —— 8 个名义仓位实为对单一事件 8 倍下注，一个结果结算即整组清零 | 新增 `max_exposure_per_neg_risk_group_usd = 4.0`（可用 `enforce_group_cap` 关闭）与 `group_add_usd` / `group_limit_ok` 纯函数；`check_pair` 以**尾部可选参数**接入，旧调用方零改动 |
| 13 | `pmq/execution/atomic.py`、`pmq/detect.py` | 事件组维度未进入执行与扫描链路 | 执行器新增内部台账 `group_exposure_usd` + 基线注入 / 释放接口，成交即记账、下单前判超限（超限直接 REJECT 且不产生任何 POST）；检测器按 `condition_id` 累计可执行机会，同组打满则拒绝后续 |

### 测试状态
- 修复前：**89 passed / 3 failed**
- 修复后：**110 passed / 1 skipped**（skip 的是需要本地看板在线的集成测试）

### 事件组闸门的用法
```python
ex.set_group_exposure_baseline(condition_id, usd_from_chain_reconcile)   # 链上对账注入基线
res = ex.execute(..., group_key=condition_id)                           # 本笔也会判超限
ex.release_group_exposure(condition_id, usd)                            # 结算后释放
```
默认上限 `4.0U`，一笔约 `2.94U`，因此同事件组通常只能放一行 —— 这是**有意的集中度约束**，
要放开需显式调大 `max_exposure_per_neg_risk_group_usd`。

---

## 三、实盘阻塞现状（2026-11-15 20:04 复核）

### ✅ 已解除（2 项）

**1. 地理围栏拦截 —— 已解除**
- 早期实测：`{"blocked":true,"ip":"199.195.250.184","country":"US","region":"NJ"}` → `doctor: NOT READY`
- **2026-11-15 20:04 起出口已变为越南**：`{"blocked":false,"ip":"157.10.52.135","country":"VN"}` → `doctor: OK`
- 含义：换到非美出口后 Polymarket 不再拒绝。**出口 IP 必须稳定保持为该 VN 出口**，若回落到美国出口会立即重新被拦。

**3. CLOB 出口不稳定 —— 已解除**
- 早期 `clob.polymarket.com/time` 时通时断（`SSL: UNEXPECTED_EOF_WHILE_READING`），经本地代理 `127.0.0.1:10808` 时不稳。
- 现 VN 出口下：CLOB API 1829ms / Gamma API 1421ms / Polygon RPC 1811ms，三者均通畅。

### 🔴 仍未消除（2 项，均需用户在链上操作）

**2. 账户无资金** —— 链上实测 `USDC.e = $0.00`（Polygon 链）。
  注：`live_state.json` 里的 `proxy_pusd 1.72` 是 FUNDER 代理地址的余额，与钱包
  `0x514f13a326a93aeda4522b973707e8602c2bcefd` 是两回事；且 1.72U 连一对最小腿
  `$1.15 × 2 = $2.30` 都不够。

**2b. USDC.e 未授权** —— 链上实测 `Polymarket CTF Exchange allowance = $0.00`。
  需先对 CTF Exchange 合约做 `approve`（额度建议 ≥ $100）。

> 这两项是纯链上操作，代码侧修不了。注入资金并 approve 后，
> 加上第 4 项（live 入口接线）即可真正实盘。

### 🟡 工程侧未完成

**4. 执行链路未接线** —— `AtomicPairExecutor` 全仓库零实例化，`sdk_adapter` 的订单心跳仍是占位。
  这是"满足实盘要求"的最后一段代码工作（优先级见第四节第 4 项）。

---

## 四、下一步优先级

1. ~~修 `_unwind` 裸敞口隐瞒~~（已完成）
2. ~~修 NO_PROXY 导致 SDK/httpx 崩溃~~（已完成）
3. ~~死人开关优雅停机语义 + 接进执行器~~（已完成）
4. **把 `AtomicPairExecutor` + `HeartbeatLoop` 接进真实 live 入口**（未做 —— 最后一段工程）
5. **对账 / reconcile 在 live 路径上的接线**（`reconcile_paper.py` 只服务纸面）
6. ~~neg-risk 事件组敞口上限~~ ✅ 已落地（#12 / #13），执行器与检测器双侧生效
7. BTC 连续两窗口亏损：需分层归因（逆选 markout 占多少 / 结算口径占多少）

---

## 五、复查命令速查

```bash
python -m pytest -q                              # 应 96 passed / 1 skipped
python -m pmq.cli doctor                         # 实盘就绪体检(当前 NOT READY: geoblock US)
python -m pmq.cli scan --limit 40                # 挂单套利闸门通过率(当前 0/40)
python tools/check_wallet_readiness.py           # 钱包/授权/凭证派生自检(只读)
python scripts/compare_2h_cycles.py              # 两小时周期盈亏与配对率
python -m pmq.crypto_5m_engine --coins btc --capital 30 --budget 3 --duration 30
```
