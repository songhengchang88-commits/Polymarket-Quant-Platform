# 开源项目复盘 → 可实盘落地的执行内核

调研时间：2026-11-02。范围：Polymarket CLOB 生态中维护活跃、结构清晰的开源实现。
本文件只记录**能被代码验证**的经验，不复述营销话术。

---

## 一、调研清单与可信度

| 项目 | 定位 | 我实际读到的内容 | 可借鉴度 |
|---|---|---|---|
| [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) | maker-only 做市，CLOB V2，政治盘 | 完整 README（架构、策略、风控、CLI） | ★★★★★ 最接近工程范本 |
| [miladhist/polymarket-market-maker](https://github.com/miladhist/polymarket-market-maker) | 做市 + 风控 + WebSocket | 完整 README | ★★★★☆ 风控分层清晰 |
| [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | 官方 Python SDK | **本机已装 0.34.6，直接读源码签名** | ★★★★★ 唯一权威 |
| [singhparshant/Polymarket](https://github.com/singhparshant/Polymarket) | Rust 做市/套利实验 | 仅检索摘要 | ★★☆☆☆ 思路参考 |
| [LuciferForge/polymarket-btc-autotrader](https://github.com/LuciferForge/polymarket-btc-autotrader) | BTC 自动套利+狙击 | 仅检索摘要 | ★☆☆☆☆ 未验证 |
| [0xrsydn/polymarket-crypto-toolkit](https://github.com/0xrsydn/polymarket-crypto-toolkit) | 插件化策略+回测 | 仅检索摘要 | ★★☆☆☆ 结构参考 |

> 标"仅检索摘要"的，我没有逐行读代码，不把它们当结论来源。

---

## 二、官方 SDK 的真实约束（读源码得到，不是转述）

本机 `py_clob_client 0.34.6` 实测签名：

```python
OrderType            = FOK | FAK | GTC | GTD
OrderArgs(token_id, price, size, side, fee_rate_bps=0, nonce=0, expiration=0, taker=...)
post_order(order, orderType="GTC", post_only=False)
get_tick_size(token_id) -> "0.1" | "0.01" | "0.001" | "0.0001"
get_fee_rate_bps() / get_neg_risk() / get_order(id) / cancel(id) / post_orders([...])
```

这三条直接决定了工程做法：

1. **tick size 每个 token 不同**——写死 `0.01` 会在低 tick 市场上被拒单或价格错位；
2. **FOK/FAK 存在**——所以"双腿原子化"是可实现的，不必接受"先成一条腿再等"；
3. **`get_order(id)` 可查成交**——所以盈亏必须来自回执，而不是下单时的计算值。

---

## 三、八个项目里反复出现的七条铁律

### 1. 成交回执是唯一真相
所有严肃实现都从 `orderID → get_order / user WS → size_matched` 反推盈亏。
本项目旧实现直接拿 `expected_profit_usd` 入账，这是 1682 笔"全胜"的根源。

### 2. 双腿必须原子化，且失败要补偿
poly-maker 的 ExecutionGateway + reconciler 本质是在做这件事。
工程顺序：FOK 腿一 → 校验 `size_matched` → FOK 腿二 → 任一腿不足额立即撤单/反手平掉。
**部分成交是最危险的状态**：它既不是成功也不是失败，而是裸敞口。

### 3. 成本必须显式建模
Polymarket taker 费按概率对称：`fee = shares × bps/10000 × min(p, 1-p)`。
不算这一项，"1.5% 无风险套利"在费率高的市场上会直接变负。

### 4. 盘口质量优先于信号
陈旧盘口（WS 断线）、交叉盘口（`bid0+bid1 ≥ 1`）、真空盘口（价差 > 5 美分）
都会让"看着很好的机会"在成交瞬间消失。poly-maker 用 "WS-staleness halt" 直接停牌。

### 5. 风控是分层预算，不是单点阈值
miladhist 的 RiskManager 与 poly-maker 的 RiskManager 都有四层：
单市场名义上限 / 事件组 worst-case 上限 / 总敞口上限 / 当日亏损熔断。
单一阈值挡不住组合风险。

### 6. 心跳与可取消性是最后一道防线
poly-maker 的 exchange heartbeat（dead-man switch）意味着：进程死了，挂单自动撤。
没有它，"程序崩了但单还挂在市场上"是必然事件。

### 7. 策略是纯函数，I/O 全部包在外层
`(book, inventory, params, clock) → TargetQuotes`。
这条决定了能不能写离线单测——没有离线测试的做市系统，等于没有刹车。

---

## 四、反面教材：这些说法在开源里都带着免责声明

- 号称"100% 胜率 ARB"的项目，README 里依然写着可以亏钱；
- miladhist 给出的现实预期是 **日收益 0.1–0.5%，胜率 60–70%**；
- poly-maker 明确写 "can lose money… test in `--paper` first"。

任何声称"零风险、必赚、胜率 100%"的实现，问题一定在**度量**，不在策略。

---

## 五、本项目已补齐 / 仍缺什么

| 能力 | 旧状态 | 现在 |
|---|---|---|
| 盈亏来源 | 模型值 `expected_profit_usd` | 成交回执推导（`pmq/execution/atomic.py`） |
| 双腿原子性 | 顺序 GTC，无校验 | FOK + 部分成交补偿 + UNHEDGED 标记 |
| 成本模型 | 仅 0.002U gas | `pmq/fees.py`：费率 + gas，可配置 |
| 盘口质量 | 无校验 | `pmq/risk.py`：陈旧/交叉/极值/价差 |
| 风控分层 | 单笔 2.6U | 单笔 + 总敞口 + 日亏熔断 + 单腿红线 |
| 可观测 | 直接 print | `pmq/journal.py`（fsync + schema）+ `pmq/metrics.py`（回撤/夏普/盈亏比） |
| 离线测试 | 无 | 39 个 pytest（全绿） |
| 心跳 dead-man | 无 | ❌ 未做 |
| 挂单对账 reconcile | 无 | ❌ 未做 |
| neg-risk 事件组上限 | 无 | ❌ 未做 |

---

## 六、真实基线（本机 40 个市场实测）

```
python -m pmq.cli scan --limit 40
扫描 40 个市场, 用时 4.1s, 通过全部闸门: 0
闸门通过率: 0.00%
拒绝原因分布:
  - 配对成本: 37     # ask0 + ask1 ≈ 1.01，taker 侧天然为负
  - 极端价位: 20
  - 盘口缺失: 3
  - 价差: 3
```

**这是最重要的一个数字。** 在成交量前 40 的市场上，`ask0+ask1 ≤ 0.985` 的机会为 0。
旧系统之所以"天天成交"，是因为它根本没在比较真实双腿——`(ask-bid)/bid` 在 0.001 上
产出了 100% 的假价差。

结论：**taker 平价套利在当前市场几乎不存在**，可持续的 edge 必须来自
①做市的价差捕获 + ②流动性激励/返佣 + ③事件驱动的短时错价。
这三条都比"捡 0.985 的便宜"难得多，也都需要上面七条铁律兜底。

---

## 七、上线检查清单（缺一项就别开真钱）

1. `python -m pmq.cli doctor` 全 PASS（含 geoblock）；
2. 目标 token 的 `tick_size` 逐一下载并缓存，不写死；
3. USDC 授权（allowance）已确认，余额 = CLOB 侧余额而非链上猜测；
4. 单腿金额 ≥ $1.00 官方门槛，且 ≥ 代码里的 1.15 缓冲；
5. 费率从 `get_fee_rate_bps()` 读取，不硬编码；
6. 当日亏损熔断阈值已设，且能被一次真实成交触发验证；
7. 账本落盘路径已确认可写（`data/pmq_journal.jsonl`）；
8. 先用 $5–10 跑 24 小时，比对 `pmq status` 与钱包实际余额；
9. `cancel_all` 的调用路径已演练过一次；
10. 心跳/死亡开关上线前补上（当前未实现，属已知缺口）。

---

## 八、不承诺的东西

代码能保证：不满足条件不下单、不成对不记账、异常不穿透。
代码**不能**保证：赚钱、正收益、跑赢手续费。
把"不能亏"翻译成工程语言，就是**把不可控敞口堵在门外**——仅此而已。
