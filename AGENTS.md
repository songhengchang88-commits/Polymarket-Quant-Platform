# 项目智能体运行规范与协同引导 (Project Agents Directive)

> **断线恢复与自启动第一指令**：
> 当会话断线重连、环境重置或开始新任务时，**必须第一时间读取 `docs/memories/` 目录中的全部版本化记忆**，以最高优先级恢复集群分工状态！

---

## 📚 仓库核心记忆索引 (Memory Index)

本仓库的持久化智能体记忆位于 `docs/memories/`：
1. **[核心行为准则与最高纲领](docs/memories/chinese_and_github_core.md)**：任务前置 GitHub 调研 · 极省 Token · 纯中文规范 · 白嫖第一铁律。
2. **[REA 逆向工程与实战法则](docs/memories/rea_reverse_engineering_memory.md)**：万物皆可逆 · morluto/rea · leanrea 切片 · 私有协议与二进制无源码平替。
3. **[多子系统协同与各自分工规范](docs/memories/subsystems_orchestration.md)**：OpenHands/MetaGPT/CrewAI 集群协同 · 五大子系统矩阵 · Lean Handoff 转交。
4. **[视听工业与短剧记忆库](docs/memories/ai_music_short_drama_memory.md)**：MoneyPrinterTurbo / VideoLingo / 跨时空视听工业工程。
5. **[Polymarket 实盘就绪审计与缺陷修复](docs/memories/live_readiness_findings.md)**：系统真实形态（纸面研究态，非已实盘）、13 项已修缺陷、4 项未消除实盘阻塞、复查命令速查。**做 Polymarket 相关任务前必读。**

---

## ⚠️ Polymarket 项目专属记忆入口

本项目（Polymarket-Quant-Platform）开工前**先读**
[`docs/memories/live_readiness_findings.md`](docs/memories/live_readiness_findings.md)：
已修缺陷清单、真实实盘阻塞（geoblock US / USDC $0 / allowance $0 / 执行链路未接线）、
下一步优先级、以及全部复查命令。

## 👥 集群各自分工速查 (Subsystem Roles Quick Reference)

在执行任何复杂任务时，智能集群自动解构为五大分工矩阵：
- **`SubSys-Dispatch`**：中枢调度与状态路由，负责任务解构与最终验收交付。
- **`SubSys-Research`**：前置扫描 GitHub 顶流方案，提供最强成熟参考，拒绝重复造低劣轮子。
- **`SubSys-Architect`**：工程落地、量化策略设计、高内聚低耦合模块研发与 ACI 自动化自测。
- **`SubSys-Reverse`**：REA 战术逆向、协议抓包、签名算法反编译与零成本免鉴权平替实现。
- **`SubSys-Critic`**：极省 Token 审计、双盲代码交叉质询与反思安全护栏。

---

## ⚡ 协作交付红线 (Operational Constraints)
1. **单次批处理聚合**：多条命令尽量单次 Shell 批处理（用 `;` 连接），大幅减少交互轮次与上下文消耗。
2. **切片抽取不 Dump**：查阅代码严格使用行号、局部正则切片（`rg -o`），绝不全量 Dump 大文件。
3. **规范中文**：全流程思考、代码注释与对外汇报一律使用准确的简体中文。
