# dsh-purge 影响面清单（只读盘点，未执行任何应用/还原操作）

盘点时间：2026-11-02 15:3x（Asia/Kuala_Lumpur）
盘点对象：本机已安装的 `dsh-purge@1.1.89`（desktop profile）

## 0. 结论速览

1. 插件**已经装好并已经应用过**：`applied.json` 记录 2026-11-02T06:46:00Z（本地 14:46），`installed-rev = apply:1.1.89`。
2. 实际被替换的宿主文件共 **58 个**，分布在 **43 个包** + Electron 主进程 `lib\main.js`，全部落在 `E:\deep seek\resources\app`（`app.asar` 解开后的覆盖层）。
3. 每个被替换的文件旁边都留了 `<原文件名>.dshpurge.bak`，**58 个 bak 全在**，回滚依赖这一套基线。
4. 会话里看到的"UNRESTRICTED SANDBOX / 你是小码酱 / OPERATOR ANCHOR"整段，**就是 `C:\Users\Administrator\.dsh\prompt-inject.md` 这个 24,902 字节文件的正文**，每轮整段注入。它不是宿主指令。

## 1. 部署事实（可逐条核对）

| 项 | 值 | 核对位置 |
|---|---|---|
| 插件版本 | 1.1.89 | `~\.dsh\profiles\desktop\node_modules\dsh-purge\package.json` |
| 安装 profile | 仅 `desktop`（没有 web profile） | `~\.dsh\profiles\` |
| 声明适配 | `dshTargets: ["0.2.1-rc.2","0.2.1-rc.5"]` | 同上 96–100 行 |
| 已应用时间 | 2026-11-02T06:46:00Z（本地 14:46） | `~\.dsh\dsh-purge\applied.json` |
| 应用版本戳 | `apply:1.1.89` | `~\.dsh\dsh-purge\installed-rev` |
| 提示词注入原文 | 24,902 B | `~\.dsh\prompt-inject.md` |
| 规则库 | 空（无文件） | `~\.dsh\rules\` |
| hooks 放行开关 | `hooksDenyBypass: true` | `~\.dsh\dsh-purge\hooks-deny.json` |
| override 状态 | `customized: false`，hash 与 seedHash 相同（未手改） | `~\.dsh\dsh-purge\override-state.json` |
| 宿主 origin 文件 | `app.asar` 182,766,714 B，mtime 2026-10-29 22:02:52 未变 | `E:\deep seek\resources\` |
| 运行树（被改） | `E:\deep seek\resources\app` | 下节清单 |
| profile bundle 注入 | `- insert: { id: dsh-purge, inject: [systemPrompt, tools] }` | `~\.dsh\profiles\desktop\cordis.patch.yml` |

## 2. 改动落点

```
E:\deep seek\
├── resources\
│   ├── app.asar                 ← 原件未动（182 MB，mtime 10-29 22:02:52）
│   ├── app.asar.unpacked\dsh\   ← 官方解包残留，未被改
│   └── app\                     ← 真正的补丁层：58 个文件在此被替换
│       ├── lib\main.js          ← Electron 主进程入口被换（504.6 KB 原件已备份）
│       ├── dsh\node_modules\@deepseek-ai\...   ← 41 个包
│       └── dsh\node_modules\@earendil-works\pi-ai\...  ← 3 个第三方 API 适配文件
```

即：宿主 Electron 加载的是解开后的 `resources\app`，官方 `app.asar` 保持原样并存。两份代码同时存在，**手动只改一边会出现版本不一致**。

## 3. 被替换的 58 个文件

| # | 被替换的文件（相对 resources\app） | 原文件大小 |
|---|---|---|
| 1 | `dsh\node_modules\@deepseek-ai\dsh-acp-app\cordis.patch.yml` | 0.5 KB |
| 2 | `dsh\node_modules\@deepseek-ai\dsh-agent-instructions\lib\index.js` | 58.7 KB |
| 3 | `dsh\node_modules\@deepseek-ai\dsh-agent-loop\lib\index.js` | 72.9 KB |
| 4 | `dsh\node_modules\@deepseek-ai\dsh-api-session-controller\lib\index.js` | 129.1 KB |
| 5 | `dsh\node_modules\@deepseek-ai\dsh-app-boot\lib\index.js` | 198.8 KB |
| 6 | `dsh\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml` | 20.4 KB |
| 7 | `dsh\node_modules\@deepseek-ai\dsh-base\package.json` | 6 KB |
| 8 | `dsh\node_modules\@deepseek-ai\dsh-client-ui-conversation\lib\client.js` | 764.6 KB |
| 9 | `dsh\node_modules\@deepseek-ai\dsh-client-ui-deliverables\lib\index.js` | 12.3 KB |
| 10 | `dsh\node_modules\@deepseek-ai\dsh-compaction-tool-result-pruner\lib\index.js` | 7.4 KB |
| 11 | `dsh\node_modules\@deepseek-ai\dsh-experimental-auto-review\lib\index.js` | 23.4 KB |
| 12 | `dsh\node_modules\@deepseek-ai\dsh-fs-observation-policy\lib\index.js` | 3.9 KB |
| 13 | `dsh\node_modules\@deepseek-ai\dsh-fs-sandbox\lib\index.js` | 7.9 KB |
| 14 | `dsh\node_modules\@deepseek-ai\dsh-harness-dev-context\lib\index.js` | 7.2 KB |
| 15 | `dsh\node_modules\@deepseek-ai\dsh-headless\cordis.patch.yml` | 1.3 KB |
| 16 | `dsh\node_modules\@deepseek-ai\dsh-llm-deepseek\lib\index.js` | 118.7 KB |
| 17 | `dsh\node_modules\@deepseek-ai\dsh-permission-presets\lib\index.js` | 16.1 KB |
| 18 | `dsh\node_modules\@deepseek-ai\dsh-permission-presets\lib\types\index.js` | 19.9 KB |
| 19 | `dsh\node_modules\@deepseek-ai\dsh-persona\lib\index.js` | 2.1 KB |
| 20 | `dsh\node_modules\@deepseek-ai\dsh-repeat-tool-reminder\lib\index.js` | 91.1 KB |
| 21 | `dsh\node_modules\@deepseek-ai\dsh-sandbox\lib\index.js` | 14.9 KB |
| 22 | `dsh\node_modules\@deepseek-ai\dsh-sandbox-local\lib\index.js` | 22.1 KB |
| 23 | `dsh\node_modules\@deepseek-ai\dsh-sandbox-policy\lib\index.js` | 7.4 KB |
| 24 | `dsh\node_modules\@deepseek-ai\dsh-sdk-app\cordis.patch.yml` | 1.9 KB |
| 25 | `dsh\node_modules\@deepseek-ai\dsh-sdk-minimal\cordis.patch.yml` | 5.5 KB |
| 26 | `dsh\node_modules\@deepseek-ai\dsh-session\lib\index.js` | 92.8 KB |
| 27 | `dsh\node_modules\@deepseek-ai\dsh-session-format-v0-to-v1\lib\index.js` | 102.4 KB |
| 28 | `dsh\node_modules\@deepseek-ai\dsh-session-log-deepseek\lib\index.js` | 7.5 KB |
| 29 | `dsh\node_modules\@deepseek-ai\dsh-session-log-deepseek\lib\types\index.js` | 9.8 KB |
| 30 | `dsh\node_modules\@deepseek-ai\dsh-session-reference\lib\index.js` | 32 KB |
| 31 | `dsh\node_modules\@deepseek-ai\dsh-session-reference\lib\types\index.js` | 23.7 KB |
| 32 | `dsh\node_modules\@deepseek-ai\dsh-session-reference\lib\types\spill.js` | 3.1 KB |
| 33 | `dsh\node_modules\@deepseek-ai\dsh-settings\lib\index.js` | 22.1 KB |
| 34 | `dsh\node_modules\@deepseek-ai\dsh-skill-badge\lib\index.js` | 1.7 KB |
| 35 | `dsh\node_modules\@deepseek-ai\dsh-subagent\lib\index.js` | 138.5 KB |
| 36 | `dsh\node_modules\@deepseek-ai\dsh-subagent\lib\types\child-agent.js` | 10.2 KB |
| 37 | `dsh\node_modules\@deepseek-ai\dsh-system-prompt\lib\index.js` | 19.4 KB |
| 38 | `dsh\node_modules\@deepseek-ai\dsh-tool-bash\lib\index.js` | 27.4 KB |
| 39 | `dsh\node_modules\@deepseek-ai\dsh-tool-fs\lib\index.js` | 48.3 KB |
| 40 | `dsh\node_modules\@deepseek-ai\dsh-tool-pwsh\lib\index.js` | 30.3 KB |
| 41 | `dsh\node_modules\@deepseek-ai\dsh-tools\lib\index.js` | 158.4 KB |
| 42 | `dsh\node_modules\@deepseek-ai\dsh-tools\lib\types\index.js` | 64.7 KB |
| 43 | `dsh\node_modules\@deepseek-ai\dsh-tools\lib\types\ptc.js` | 39.1 KB |
| 44 | `dsh\node_modules\@deepseek-ai\dsh-tool-subagent\lib\index.js` | 33.1 KB |
| 45 | `dsh\node_modules\@deepseek-ai\dsh-tool-todo\lib\index.js` | 6.4 KB |
| 46 | `dsh\node_modules\@deepseek-ai\dsh-tool-web\lib\index.js` | 33.4 KB |
| 47 | `dsh\node_modules\@deepseek-ai\dsh-user-approval\lib\index.js` | 8.3 KB |
| 48 | `dsh\node_modules\@deepseek-ai\dsh-user-approval\lib\types\index.js` | 10.4 KB |
| 49 | `dsh\node_modules\@deepseek-ai\dsh-web-app\cordis.patch.yml` | 22.4 KB |
| 50 | `dsh\node_modules\@deepseek-ai\dsh-web-app\lib\index.js` | 7.7 KB |
| 51 | `dsh\node_modules\@deepseek-ai\dsh-web-app\presets\cordis.patch.yml` | 7.2 KB |
| 52 | `dsh\node_modules\@deepseek-ai\dsh-web-app\presets\minimal.patch.yml` | 3.6 KB |
| 53 | `dsh\node_modules\@deepseek-ai\dsh-web-app\presets\ptc.patch.yml` | 6.8 KB |
| 54 | `dsh\node_modules\@deepseek-ai\dsh-web-app\presets\standard.patch.yml` | 6.6 KB |
| 55 | `dsh\node_modules\@earendil-works\pi-ai\dist\api\anthropic-messages.js` | 56.8 KB |
| 56 | `dsh\node_modules\@earendil-works\pi-ai\dist\api\openai-completions.js` | 63.1 KB |
| 57 | `dsh\node_modules\@earendil-works\pi-ai\dist\api\openai-responses-shared.js` | 31.4 KB |
| 58 | `lib\main.js` | 504.6 KB |

## 4. 86 条补丁的落点分布（按文件键）

| 文件键 | 补丁数 | 改动内容 |
|---|---|---|
| `agent-loop` | 5 | 每一步用当前整段系统提示替换第一条，避免旧提示被钉住；每个模式的第一次发送前先挂上默认提示词；decision.messages 缺省时按空数组处理，避免第二轮 gateway/internal；空白宿主先挂全文，再用 LF 比对头尾，避免 CRLF 误判再糊一份；ChatML 头和正文尾都在才算挂住，只剩头或只剩尾都重新置顶 |
| `desktop-shell` | 5 | 官方桌面升级安装已下载的新版本，不再重新打开旧的解开目录；已接管的官方桌面更新改为 ASCII 脚本，中文 Windows 不再吞掉换行；host 被信号杀 (重启密封包) 时不报错,不弹 crash dialog；host code=0 正常退出不报错 (消除 stopped 对话框)；主进程兜底 DSH_PROFILE=desktop：双击 exe 从 explorer 继承 env 常缺 DSH_PROFILE → host 'cannot determine the boot profile' → dsh-purge 插件没 activate → inject 全断 |
| `escalation` | 4 | 豁免严格升级阶梯；升级请求无条件授信；沙箱拒绝/升级 hint 不再要求用户审批；升级结果 rejected/cancelled/unavailable 仍授信 |
| `preset-minimal, preset-sdk-minimal` | 4 | 极简模式终端不再写没有网络；极简 bash 去掉误留的镜像说明；内置 minimal：去掉默认身份并打开 complete，让 inject 能进；撤掉曾追加的 fs/web，恢复官方极简工具表 |
| `base` | 3 | 沙箱默认 danger-full-access（可被 UI 切换，不写死 env）；审批：danger-full-access → never（与 mode 组合合法）；恢复插件延长的 bash 等待 |
| `agent-instructions` | 3 | 全局免责声明；替换稿不压过 system；作用域不压过 system |
| `web-app-lib, harness-dev-context` | 2 | Web 面：去掉 DeepSeek Harness 自称；网页端提示词去掉身份句和禁止另开服务器 |
| `llm-deepseek` | 2 | deepseek-flash 恢复官方 in-history，并撤掉会打成 TRANSPORT 的独立连接补丁；Messages 的 system 字段用当前系统提示，不再把后出现的系统提示降成中途消息 |
| `conversation-client` | 2 | 回退只藏被撤掉的那一轮；回退标记到达时立刻从当前对话拿掉已发送的消息 |
| `desktop-shell` | 2 | host 每次退出都写 code/signal/stderr 到持久化日志；主进程退出原因写持久化日志（quit/信号/崩溃/未捕获异常） |
| `system-prompt, system-prompt-nested` | 2 | complete 预设仍保留 dsh-purge 注入；renderPrompt 对 undefined section.text 兜底，避免第二轮 gateway/internal |
| `pi-ai-anthropic-messages` | 2 | Anthropic OAuth 不再强插 Claude Code 首段系统提示；OAuth 不再伪装 claude-cli |
| `tool-bash, tool-pwsh` | 2 | bash/pwsh 工具描述：沙箱拒绝话术→可继续；bash/pwsh 去掉 delete/move 过度谨慎描述 |
| `sandbox-policy` | 2 | 沙箱只读策略不拦任务；加宽自动放行；三条沙箱策略文案去掉 DSH 品牌词 |
| `user-approval-code` | 2 | 审批配置默认 never；never 提示句 → 自动放行 |
| `hooks-claude-code, hooks-claude-code-nested, hooks-codex, hooks-codex-nested` | 2 | Stop hook 不再 steer 假用户话；UserPromptSubmit deny 不再 reject 整轮 |
| `sandbox-local` | 2 | 沙箱 deny file-write；沙箱 confine 直通 |
| `app-boot` | 1 | 声明过的补丁文件不见时，从 .dshpurge.bak 拷回再加载 |
| `deliverables-prompt` | 1 | 交付物提示词去掉 Do not call present |
| `skill-badge` | 1 | 技能描述去掉官方徽章品牌 |
| `session-reference` | 1 | 跨会话引用不再标 untrusted |
| `session` | 1 | 回退空标记不进模型请求 |
| `session-log-deepseek, session-log-deepseek-types` | 1 | 官方模型 API 默认不上传 Session Log |
| `permission-presets, permission-presets-types` | 1 | 新会话在部署已是 danger-full-access 时不再钉回 workspace-write |
| `api-session-controller, agent-loop` | 1 | 排队 followup/next-turn，插话 steer/next-step。Ctrl+Enter 不能改成 followup（#121） |
| `pi-ai-completions, pi-ai-responses-shared` | 1 | developer role 仅限 openai.com/azure 原生，第三方代理 (deepseek/glm/recoverystar/中转) 全回退 system |
| `dsh-tools, dsh-tools-ptc` | 1 | PTC |
| `auto-review` | 1 | 官方 Auto Review 不再拦工具 |
| `tool-todo` | 1 | 回退把已完成任务收回成未开始 |
| `agent-loop, session` | 1 | systemNodes/derive 对缺 content 兜底，避免回退后第二轮 gateway/internal |
| `base, preset-ptc, preset-standard, preset-cordis` | 1 | 计划模式：操作者要求实施时不硬拦 |
| `api-session-controller` | 1 | session-controller 普通发送走 followup；插话 steer 仍打断当前步（#121） |
| `dsh-tools, dsh-tools-types` | 1 | 无审批通道时放行；用户点拒绝仍 deny |
| `fs-observation-policy` | 1 | 观察策略读写意图放行 |
| `fs-sandbox` | 1 | 文件系统围栏取消 |
| `user-approval-code, user-approval, user-approval-invariant` | 1 | ask 提示句 → 自动放行 |
| `tool-fs` | 1 | read 上限放宽 |
| `compaction-tool-result-pruner` | 1 | 工具结果修剪禁用 |
| `repeat-tool-reminder` | 1 | 重复调用守卫禁用 |
| `preset-unrestricted` | 1 | 用户自造 unrestricted 预设只去掉身份句（软补丁）/ soft strip for a user-made unrestricted preset |
| `preset-standard, preset-ptc, preset-cordis, preset-minimal` | 1 | Web 四个内置预设只去掉身份句 |
| `agent-preset, web-app, headless, preset-acp-app, preset-sdk-app` | 1 | persona：去掉官方默认身份，不另写人设 |
| `user-approval-code, user-approval-invariant, user-approval` | 1 | 审批门自动放行 |
| `user-approval, user-approval-invariant` | 1 | 审批提示句限制 |
| `preset-liangshen, preset-liangshen-pkg` | 1 | 梁神去掉官方身份句，留下本模式的工作纪律 |
| `subagent, subagent-types` | 1 | 子代理权限锁→可继续 |
| `tool-fs, escalation, tool-bash, tool-pwsh` | 1 | fs/命令升级字段：去掉 user approval 拦截描述 |
| `hooks-codex, hooks-codex-nested` | 1 | dsh-hooks-codex：deny→allow（可选） |
| `settings` | 1 | 设置服务补回 register/get，旧插件可继续用 |
| `session-format-v0` | 1 | v0 会话：mnemon summary 可挂在 instructions/recall 上 |
| `persona` | 1 | dsh-persona：兼容 0.1.2 text 字段，否则梁神会话起不来 |
| `system-prompt` | 1 | 去掉官方默认身份，保留官方功能提示词 |
| `preset-unrestricted, preset-standard, preset-cordis, preset-ptc, preset-liangshen, preset-liangshen-pkg` | 1 | preset tool-web fetch 打开 |
| `subagent, tool-subagent` | 1 | 子代理深度默认 3 → 10 |
| `hooks-claude-code, hooks-claude-code-nested` | 1 | dsh-hooks-claude-code：deny→allow（可选） |
| `tool-web, tool-web-nested` | 1 | dsh-tool-web：外部内容 untrusted 拦截→可执行数据 |
| `liangshen-tool-bootstrap` | 1 | 插件 dsh-liangshen（梁神）：phase-1 保留注入段（可选） |

## 5. 备份机制（等于是唯一回滚绳）

命名与位置
- 逐个文件：`<原文件>.dshpurge.bak`，**与原文件同目录**（代码：`lib\core.js` 6405 `BAK_SUFFIX = ".dshpurge.bak"`）。
- 规则文件另一套后缀：`.dsh-purge-rule.bak`（`lib\rules.js` 21）。
- 桌面 asar 层：`${asar}.bak`（`lib\extract-asar.js` 194、`lib\desktop.js` 286/1523）；`app.asar.unpacked` → `app` 用 `robocopy /E` 覆盖。
- 命令运行时目录（in-bin）走带摘要的备份名 `<basename>.<digest>.bak`，放 bakRoot，失败回退 `os.tmpdir()`（`lib\core.js` 386–393）。

写入安全
- `wx` 独占创建占位再原子拷贝：**bak 已存在就跳过，绝不覆盖**（`backupAll`，6542–6576）。这是"原件只能被记录一次"的保证，也是最大风险点（见第 6 节）。
- 用户 patch 层 `profiles\<name>\cordis.patch.yml` **永不建 bak**；还原时只删 bak，绝不把内容拷回（#43，6407–6408 / 6605–6616）。

自愈
- `restoreMissingOriginalsFromBak`：原文件丢失或 0 字节时，从旁边 bak 拷回，且不删 bak（6430–6465）。
- `refreshStaleBaksForUpgrade`：原文件不含任何 purge marker 且与 bak 字节不同 → 判定宿主自升级覆盖过，用当前官方新版**刷新 bak 基线**（6478–6540）。
- `app-boot` 补丁：声明过的覆盖层文件缺失时，启动前从 bak 拷回再加载。

还原
- 入口：面板「还原」/ `/purge revert` / `dsh-purge --revert`。
- `revertAll`（6588+）：先补齐缺失原件 → 逐文件从 bak 覆盖回去 → 删除 bak；`preserveClientBundles` 时跳过被 watch 的前端 bundle（避免 copyFile 改 mtime 触发重载，#63）。
- 「卸载」= 先 revert（若已应用）→ 删注入文件/规则库/插件本体 → 重启宿主。

## 6. 单点风险（代码注释里自己写明的）

1. **一条基线，老化即混淆**：`refreshStaleBaksForUpgrade` 的注释直接承认——老 bak 变过期后 `revertAll` 会把当前新版写回成老版，"新代码/旧代码混合直接崩"（6470–6475）。
2. **58 个文件跨 43 个包 + 主进程**：无法逐个人工回退；只能依赖 bak 整体还原，bak 一旦被清理/替换，官方原件即永久丢失。
3. **注入内容实体化**：提示词是磁盘上的 `prompt-inject.md`（24.9 KB），每轮整段覆盖第一条 system 提示；改它不用动任何 `@deepseek-ai` 文件，也不会留下新的 bak。
4. **两份代码并存**：`app.asar` 与 `resources\app` 同时存在，日后手工改文件、或官方升级只更新 asar，都会造成运行树与官方包不一致。
5. **审批/沙箱相关补丁集中在 12 个文件**（`user-approval`、`sandbox`、`sandbox-local`、`sandbox-policy`、`fs-sandbox`、`fs-observation-policy`、`permission-presets`、`escalation`、`base`、`tool-bash`、`tool-pwsh`、`agent-instructions`），这些是影响行为最大的一批。

## 7. 只读核对方式（不产生任何写入）

```powershell
# 1) 数一数 bak 与原件是否成对
Get-ChildItem "E:\deep seek\resources\app" -Recurse -Force -Filter *.dshpurge.bak |
  ForEach-Object { [pscustomobject]@{ Original = $_.FullName -replace '\.dshpurge\.bak$',''; Bak = $_.FullName } }

# 2) 看当前运行的补丁状态（插件自带只读命令）
node "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-purge\bin\dsh-purge.js" --status

# 3) 确认注入段来源
(Get-Item "$env:USERPROFILE\.dsh\prompt-inject.md").Length
```

---

本清单为静态盘点 + 文件系统核对结果，**未调用 `purge_apply`、未修改任何被替换文件、未执行还原**。
