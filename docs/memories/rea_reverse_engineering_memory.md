# REA (Reverse Engineer Anything) 核心逆向记忆与实战法则

> 永久记忆固化：深度吸纳 GitHub 顶流 **morluto/rea** (Reverse Engineer Anything, ⭐338K+)、**leanrea** (8KB 极简纯 CLI 省 35% Token)、**mitmproxy** (⭐45K+)、**acheong08/ChatGPT** 与 **NSA Ghidra** (⭐91K+) 架构精髓。

---

## ⚡ 核心纲领与第一性原理 (Core Philosophy)

1. **万物皆可逆，源码在二进制与字节流中依然透明**：
   - 不依赖原始源码，通过运行时探测、字节码反编译、AST 还原、私有协议抓包与符号表切片，彻底吃透目标软件的底层架构与业务逻辑。
2. **极省 Token 与局部切片原则 (Lean Slice)**：
   - 严禁全量 Dump 反编译工程或大体积反汇编清单；
   - 严格采用“入口定位 ➔ 调用链裁剪 ➔ 关键算法正则切片”三步法，Token 开销比全量丢入降低 35%~60%。
3. **证据驱动 (Evidence-Backed Analysis)**：
   - 逆向分析的每项结论必须具备确凿证据：具体函数偏移、IPC 通道名、HTTP 请求特征头、加解密算法或符号表条目。
4. **终极目标：零成本平替与生态复刻**：
   - 逆向不是目的，输出高效、健壮、免鉴权或低成本的原生 Python/Node.js 平替实现才是终极成果。

---

## 🛠️ 全领域逆向靶向矩阵与最速分析链 (Targeting Matrix)

| 目标类型 | 核心工具链与引擎 | 最速逆向路径 (极低 Token 消耗) | 平替与提炼产物 |
|---|---|---|---|
| **Web 前端 / 打包混淆** | SourceMap 还原器 / Prettier / AST 切片 | 探测 `.map` 路径还原原始目录；无 Map 则基于网络请求发起堆栈行定位路由。 | 提取未公开 API、前端验签参数、业务数据流。 |
| **Electron 应用** (`app.asar`) | `@electron/asar` / AST / Ripgrep | `asar extract` 解包 ➔ 读 `package.json` 的 `main` ➔ 正则抽取 `ipcMain.handle` 与 `mainWindow.loadURL`。 | 提取桌面端特权接口、本地配置注入、离线校验绕过。 |
| **Native 二进制 (PE/ELF/Mach-O)** | Ghidra / IDA / Hopper / dumpbin / strings | `strings -n 6` 提取端点与密钥 ➔ `dumpbin /IMPORTS` 查系统调用 ➔ Ghidra 反编译关键函数。 | 提取加密密钥、本地 License 算法、底层私有通信协议。 |
| **Go 语言程序** | `go version -m` / `go tool nm` | 一行提取所有引用的开源三方库版本与 Git Commit；通过符号表还原 Package 结构。 | 识别开源骨架，直接使用上游开源实现平替。 |
| **.NET / C# / Unity** | `ilspycmd` / dnSpy | 秒级全量导出高可读性 C# 源代码；Unity 提取 `Assembly-CSharp.dll` 核心逻辑。 | 100% 还原业务数据结构、算法逻辑与本地逻辑。 |
| **Android APK** | JADX-CLI / apktool | `jadx -d out target.apk` ➔ 针对 `Retrofit` / `OkHttp` 请求拦截器切片提取网络层。 | 提取移动端专有 API（通常比网页端风控更弱）。 |
| **网络私有协议 / API** | mitmproxy / curl_cffi / Wireshark | `mitmproxy` 抓取 HAR ➔ 分析 Header、Query、Body 签名算法 ➔ `curl_cffi` 模拟 TLS/JA3/JA4。 | 无头免浏览器轻量 Python/Node.js 客户端。 |

---

## 🔄 标准 SOP 逆向闭环工作流 (Standard Operating Procedure)

1. **Step 1: 侦测先行 (Reconnaissance)**
   - 单行命令识别架构：检查可执行格式（PE/ELF/Mach-O/Node/JVM/.NET/Python PyInstaller）。
   - 确定宿主运行时与打包壳：是否有 UPX、ASAR、WebPack、V8 字节码 (JSC/Bytenode)。
2. **Step 2: 关键路径切片 (Key-Path Slicing)**
   - 搜索高频特征：`api/`、`https://`、`Authorization`、`sign`、`token`、`secret`、`cipher`。
   - 提取最小上下文窗口：仅抽取目标函数及前后 20-50 行逻辑。
3. **Step 3: 算法复现与指纹模拟 (Replication & Impersonation)**
   - 用纯原生代码复写签名（MD5/SHA256/AES/HMAC/RSA）。
   - 配置正确的请求头与客户端 TLS/JA4 特征。
4. **Step 4: 固化交付 (Codification)**
   - 产出无外部笨重依赖、极简且高可用的脚本或模块。
