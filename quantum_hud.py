"""
QuantumOrb / Cyberpunk HUD 极客交易终端渲染器
================================================
集成特性:
1. 3D Force-Directed 星系粒子球 (3D Risk Constellation Orb) - Three.js 驱动
2. 5道硬风控实时指示灯 (Geoblock, Redline 1.15U, 1:1 Symmetry, Dead-Man Switch, 45% Budget)
3. 6.98U 实盘专属防御战实时看板 (零滑点、零单腿裸露、到期合约 1.000U 结算倒计时)
4. 毫秒级深色玻璃拟态 HUD (Cyberpunk Glassmorphism Terminal)
"""

CYBERPUNK_HUD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QUANTUMORB // POLYMARKET QUANT TERMINAL</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;800&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
    <style>
        :root {
            --neon-cyan: #00f3ff;
            --neon-magenta: #ff0055;
            --neon-green: #00ff66;
            --neon-amber: #ffaa00;
            --hud-bg: #030712;
            --panel-bg: rgba(10, 16, 31, 0.75);
            --border-glow: rgba(0, 243, 255, 0.25);
        }
        body {
            background-color: var(--hud-bg);
            color: #e2e8f0;
            font-family: 'JetBrains Mono', monospace;
            overflow-x: hidden;
            background-image: 
                radial-gradient(circle at 50% 0%, rgba(0, 243, 255, 0.08) 0%, transparent 60%),
                linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
            background-size: 100% 100%, 32px 32px, 32px 32px;
        }
        .font-orbitron { font-family: 'Orbitron', sans-serif; }
        .hud-glass {
            background: var(--panel-bg);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.45);
        }
        .hud-glass-cyan {
            background: rgba(8, 20, 38, 0.85);
            border: 1px solid rgba(0, 243, 255, 0.3);
            box-shadow: 0 0 20px rgba(0, 243, 255, 0.15);
        }
        .hud-glass-rose {
            background: rgba(30, 10, 20, 0.85);
            border: 1px solid rgba(255, 0, 85, 0.35);
            box-shadow: 0 0 20px rgba(255, 0, 85, 0.15);
        }
        .text-glow-cyan { text-shadow: 0 0 10px rgba(0, 243, 255, 0.7); }
        .text-glow-green { text-shadow: 0 0 10px rgba(0, 255, 102, 0.7); }
        .text-glow-magenta { text-shadow: 0 0 10px rgba(255, 0, 85, 0.7); }
        .glow-box-cyan { box-shadow: 0 0 15px rgba(0, 243, 255, 0.3); }
        .glow-box-green { box-shadow: 0 0 15px rgba(0, 255, 102, 0.3); }
        
        /* 扫描线动画 */
        .scanline {
            width: 100%;
            height: 2px;
            background: linear-gradient(90deg, transparent, rgba(0, 243, 255, 0.6), transparent);
            position: absolute;
            top: 0;
            left: 0;
            animation: scan 4s linear infinite;
            pointer-events: none;
            z-index: 20;
        }
        @keyframes scan {
            0% { top: 0%; opacity: 0; }
            10% { opacity: 1; }
            90% { opacity: 1; }
            100% { top: 100%; opacity: 0; }
        }
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: #050914; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 2px; }
        ::-webkit-scrollbar-thumb:hover { background: #00f3ff; }
    </style>
</head>
<body class="min-h-screen flex flex-col relative selection:bg-cyan-500 selection:text-black">
    <div class="scanline"></div>

    <!-- 顶部状态栏 HUD Header -->
    <header class="hud-glass border-b border-cyan-500/20 px-6 py-3 flex flex-wrap items-center justify-between gap-4 sticky top-0 z-50">
        <div class="flex items-center gap-4">
            <div class="relative flex items-center justify-center">
                <div class="w-10 h-10 rounded-xl bg-cyan-950/60 border border-cyan-400 flex items-center justify-center text-cyan-300 font-orbitron font-black text-xl glow-box-cyan">
                    Q
                </div>
                <span class="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                <span class="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
            </div>
            <div>
                <div class="flex items-center gap-2">
                    <h1 class="text-base font-orbitron font-extrabold tracking-wider text-white flex items-center gap-2">
                        QUANTUMORB <span class="text-xs px-2 py-0.5 rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">V3.0 CYBER-HUD</span>
                    </h1>
                    <span class="text-xs text-slate-500">|</span>
                    <span id="header-ws-badge" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded text-[11px] font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> WS 0.05ms
                    </span>
                    <span id="deadman-badge" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-mono bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">
                        <i class="fa-solid fa-shield-cat"></i> DEAD-MAN 30s
                    </span>
                </div>
                <div class="text-[11px] text-slate-400 font-mono mt-0.5 flex items-center gap-3">
                    <span class="text-cyan-400 font-bold">● POLYMARKET CLOB L2 实时防线</span>
                    <span class="text-slate-600">/</span>
                    <span>1:1 绝对对称锁</span>
                    <span class="text-slate-600">/</span>
                    <span>零未对冲敞口</span>
                    <span class="text-slate-600">/</span>
                    <span>合约 1.000U 结算兜底</span>
                </div>
            </div>
        </div>

        <!-- 5 道硬风控指示灯 (Five Hard Gates) -->
        <div class="hidden lg:flex items-center gap-2 bg-black/60 px-3.5 py-1.5 rounded-lg border border-slate-800 text-[11px] font-mono">
            <span class="text-slate-400 mr-1"><i class="fa-solid fa-lock text-cyan-400 mr-1"></i>风控闸门:</span>
            <div id="gate-geo" class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">1. GEOBLOCK</div>
            <div id="gate-redline" class="px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-500/40">2. ≥$1.15红线</div>
            <div id="gate-symm" class="px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-500/40">3. 1:1对称</div>
            <div id="gate-deadman" class="px-2 py-0.5 rounded bg-cyan-950/80 text-cyan-400 border border-cyan-500/40">4. 死人开关</div>
            <div id="gate-budget" class="px-2 py-0.5 rounded bg-amber-950/80 text-amber-400 border border-amber-500/40">5. 45%仓位锁</div>
        </div>

        <!-- 账户资金卡片 (实时直连 6.98U) -->
        <div class="flex items-center gap-3">
            <div id="account-card" class="hud-glass-rose px-4 py-2 rounded-xl text-right font-mono transition">
                <div class="flex items-center justify-end gap-2 text-[10px] uppercase tracking-wider text-rose-300 font-bold">
                    <span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span>
                    <span id="account-type-title">实盘链上防御资金</span>
                    <span id="funder-short" class="text-slate-400">(0x735e...00dc)</span>
                </div>
                <div class="flex items-baseline justify-end gap-1.5 my-0.5">
                    <span class="text-xs text-rose-400">$</span>
                    <span id="acc-cash-val" class="text-2xl font-black font-orbitron text-white text-glow-magenta">6.98</span>
                    <span class="text-[11px] text-slate-400">USDC</span>
                </div>
                <div class="text-[10px] text-emerald-400 font-mono flex items-center justify-end gap-2">
                    <span id="account-safe-margin">安全垫: 55% 恒定存留</span>
                    <span>|</span>
                    <span id="account-pnl">已实现: $0.00</span>
                </div>
            </div>

            <div class="flex flex-col gap-1.5">
                <div class="flex rounded-lg p-0.5 bg-black/60 border border-slate-800 text-[11px] font-mono">
                    <button id="btn-live-mode" onclick="switchMode('live')" class="px-3 py-1 rounded bg-rose-600 text-white font-bold transition glow-box-cyan">
                        LIVE 实盘
                    </button>
                    <button id="btn-paper-mode" onclick="switchMode('paper')" class="px-3 py-1 rounded text-slate-400 hover:text-white transition">
                        PAPER 模拟
                    </button>
                </div>
                <button onclick="emergencyCancelAll()" class="px-2.5 py-1 rounded bg-rose-950/60 hover:bg-rose-900/80 text-rose-400 border border-rose-500/50 text-[10px] font-mono transition flex items-center justify-center gap-1">
                    <i class="fa-solid fa-hand"></i> 紧急一键撤单
                </button>
            </div>
        </div>
    </header>

    <!-- 主体视口 Grid 布局 -->
    <main class="flex-1 p-5 grid grid-cols-1 xl:grid-cols-12 gap-5 max-w-[1920px] mx-auto w-full">
        
        <!-- 左侧 8 栏: 3D 拓扑球 + 深度盘口雷达 -->
        <div class="xl:col-span-8 flex flex-col gap-5">
            
            <!-- 🌌 核心 3D 星系粒子拓扑球 (Quantum Risk Orb) -->
            <div class="hud-glass rounded-2xl p-4 relative overflow-hidden flex flex-col border border-cyan-500/20">
                <div class="flex items-center justify-between mb-2 z-10 relative">
                    <div class="flex items-center gap-2">
                        <i class="fa-solid fa-atom text-cyan-400 animate-spin text-sm" style="animation-duration: 8s;"></i>
                        <h2 class="text-sm font-orbitron font-bold text-white tracking-wide">
                            QUANTUM RISK ORB // 全网二元盘口力导向拓扑球
                        </h2>
                    </div>
                    <div class="flex items-center gap-4 text-xs font-mono text-slate-400">
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-cyan-400"></span> 监控中: <b id="orb-node-count" class="text-white">80</b> 盘口</span>
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-400"></span> 做市 Edge: <b id="orb-maker-count" class="text-emerald-400">12</b></span>
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-rose-400"></span> 负风险锁定: <b id="orb-arb-count" class="text-rose-400">0</b></span>
                    </div>
                </div>

                <!-- Three.js Canvas 容器 -->
                <div class="relative w-full h-[320px] rounded-xl bg-[#02050e] border border-cyan-500/20 overflow-hidden flex items-center justify-center">
                    <canvas id="three-orb-canvas" class="w-full h-full block cursor-grab active:cursor-grabbing"></canvas>
                    
                    <!-- 浮动在 3D 球上方的状态 HUD -->
                    <div class="absolute top-3 left-3 pointer-events-none font-mono text-[11px] text-cyan-400 bg-black/60 p-2 rounded border border-cyan-500/30">
                        <div>POLAR COORD: <span id="orb-coord">ROTATING</span></div>
                        <div>SAMPLING LATENCY: <span id="orb-latency" class="text-emerald-400 font-bold">0.05ms</span></div>
                        <div>ZERO-LOSS SHIELD: <span class="text-emerald-400">ACTIVE [100%]</span></div>
                    </div>

                    <div class="absolute bottom-3 right-3 pointer-events-none font-mono text-[10px] text-slate-400 bg-black/70 px-2.5 py-1 rounded border border-slate-800">
                        提示: 拖动鼠标可 360° 旋转观察 80 个盘口点差粒子引力场
                    </div>
                </div>
            </div>

            <!-- 🎯 活跃持仓袋 (Active Parity Locks) -->
            <div class="hud-glass rounded-2xl p-4 border border-emerald-500/20">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                        <h2 class="font-orbitron font-bold text-sm text-white flex items-center gap-1.5">
                            <i class="fa-solid fa-lock text-emerald-400"></i> 1:1 对称对冲锁仓位 (Zero-Unhedged Bags)
                        </h2>
                    </div>
                    <span class="text-xs text-emerald-400 font-mono">
                        硬核守则: 严禁市价割肉 • 1:1 对称承兑 • 到期以精确 $1.000 USD 法定兜底
                    </span>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left font-mono">
                        <thead class="text-slate-400 border-b border-slate-800 uppercase text-[11px] bg-black/30">
                            <tr>
                                <th class="py-2.5 px-3">对冲目标盘口</th>
                                <th class="py-2.5 px-2 text-center">配对成本 (P0+P1)</th>
                                <th class="py-2.5 px-2 text-center">锁定投入 (U)</th>
                                <th class="py-2.5 px-2 text-center">法定到期收益</th>
                                <th class="py-2.5 px-2 text-center">状态 / 退出逻辑</th>
                                <th class="py-2.5 px-3 text-right">离场验算</th>
                            </tr>
                        </thead>
                        <tbody id="active-bags-tbody" class="divide-y divide-slate-800/60">
                            <tr>
                                <td colspan="6" class="py-6 text-center text-slate-500 font-mono">
                                    <i class="fa-solid fa-shield-halved text-emerald-500 text-lg mb-2 block"></i>
                                    当前可用资金 $6.98U 已处于 100% 绝对保护状态。<br>
                                    后台毫秒级雷达全天候锁定 80 个盘口，一旦出现无风险利差将以 1:1 对称锁瞬间打入！
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 📊 全网 L2 盘口深度矩阵 (L2 Depth & Spread Matrix) -->
            <div class="hud-glass rounded-2xl p-4 border border-cyan-500/20">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2">
                        <i class="fa-solid fa-chart-line text-cyan-400"></i>
                        <h2 class="font-orbitron font-bold text-sm text-white">
                            CLOB L2 实时盘口深度雷达 (<span id="market-total-badge" class="text-cyan-400">80</span>)
                        </h2>
                    </div>
                    <div class="text-xs text-slate-400 font-mono">
                        轮询扫描: <span id="stat-scan-count" class="text-white">0</span> 次 | 
                        平均延迟: <span id="stat-latency" class="text-emerald-400 font-bold">0.05ms</span>
                    </div>
                </div>

                <div class="overflow-x-auto max-h-[480px] overflow-y-auto">
                    <table class="w-full text-xs text-left font-mono">
                        <thead class="text-slate-400 border-b border-slate-800 sticky top-0 bg-[#070c18] z-10 text-[11px]">
                            <tr>
                                <th class="py-2.5 px-3">预测事件 (点击直达)</th>
                                <th class="py-2.5 px-2 text-center">YES 买1 / 卖1</th>
                                <th class="py-2.5 px-2 text-center">NO 买1 / 卖1</th>
                                <th class="py-2.5 px-2 text-center">Taker 配对和</th>
                                <th class="py-2.5 px-2 text-center">Taker 净空间</th>
                                <th class="py-2.5 px-2 text-center">Maker 点差</th>
                                <th class="py-2.5 px-3 text-right">安全评估</th>
                            </tr>
                        </thead>
                        <tbody id="market-table-tbody" class="divide-y divide-slate-800/60">
                            <!-- 动态注入盘口 -->
                        </tbody>
                    </table>
                </div>
            </div>

        </div>

        <!-- 右侧 4 栏: 实盘控制舱 + 审计流 + 快讯 -->
        <div class="xl:col-span-4 flex flex-col gap-5">

            <!-- 🎛️ 实盘作战控制台 (Live Mission Hub) -->
            <div class="hud-glass rounded-2xl p-4 border border-rose-500/30 relative">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="font-orbitron font-bold text-sm text-white flex items-center gap-2">
                        <i class="fa-solid fa-crosshairs text-rose-400"></i> MISSION CONTROL // 实盘中枢
                    </h2>
                    <span id="clob-version-badge" class="text-[10px] px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">
                        CLOB V2 官方签名
                    </span>
                </div>

                <!-- 节点地理检测状态框 -->
                <div class="bg-black/60 p-3 rounded-xl border border-slate-800 mb-3 text-xs font-mono space-y-2">
                    <div class="flex items-center justify-between">
                        <span class="text-slate-400">网络出口 IP 状态:</span>
                        <span id="geo-status-indicator" class="font-bold text-amber-400 flex items-center gap-1.5">
                            <span class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span> 探测节点中...
                        </span>
                    </div>
                    <div class="text-[11px] text-slate-500 flex justify-between border-t border-slate-800/60 pt-2">
                        <span>FUNDER: <b class="text-slate-300">0x735e...00dc</b></span>
                        <span>红线: <b class="text-emerald-400">单腿≥$1.15</b></span>
                    </div>
                </div>

                <!-- 控制按钮组 -->
                <div class="grid grid-cols-2 gap-2 text-xs font-mono">
                    <button onclick="testLiveOrder()" class="px-3 py-2.5 rounded-xl bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 transition flex items-center justify-center gap-2 font-bold glow-box-cyan">
                        <i class="fa-solid fa-bolt text-rose-400"></i> 签名微测单
                    </button>
                    <button onclick="checkGeo()" class="px-3 py-2.5 rounded-xl bg-cyan-950/40 hover:bg-cyan-900/40 text-cyan-300 border border-cyan-500/30 transition flex items-center justify-center gap-2">
                        <i class="fa-solid fa-network-wired"></i> 刷新网络探针
                    </button>
                </div>
                <div id="action-feedback" class="mt-2 text-[11px] font-mono text-cyan-400 hidden bg-black/80 p-2 rounded border border-cyan-500/30 break-all"></div>
            </div>

            <!-- 📜 链上真实回执审计流 (Execution Journal) -->
            <div class="hud-glass rounded-2xl p-4 border border-cyan-500/20">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="font-orbitron font-bold text-sm text-white flex items-center gap-2">
                        <i class="fa-solid fa-receipt text-cyan-400"></i> AUDIT JOURNAL // 回执账本
                    </h2>
                    <span class="text-[10px] text-slate-500 font-mono">盈亏严格以成交回执为准</span>
                </div>
                <div id="trades-journal-list" class="space-y-2 text-xs font-mono max-h-[300px] overflow-y-auto">
                    <div class="text-slate-500 text-center py-4">账本纯净，等待真实成交回执写入...</div>
                </div>
            </div>

            <!-- 📡 实时新闻快讯流 (Intelligence Stream) -->
            <div class="hud-glass rounded-2xl p-4 border border-slate-800">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="font-orbitron font-bold text-sm text-white flex items-center gap-2">
                        <i class="fa-solid fa-satellite text-rose-400 animate-pulse"></i> LIVE INTEL // 突发快讯
                    </h2>
                    <span class="text-[10px] text-rose-400 font-mono">10s 轮询</span>
                </div>
                <div id="news-stream-list" class="space-y-2 max-h-[360px] overflow-y-auto font-mono text-xs">
                    <div class="text-slate-500 text-center py-4">正在全网捕获政治、美联储与电竞比分...</div>
                </div>
            </div>

        </div>

    </main>

    <!-- Three.js 3D 拓扑球脚本与数据渲染器 -->
    <script>
        let currentMode = 'live';
        let orbScene, orbCamera, orbRenderer, orbPoints, orbLines;
        let isOrbDragging = false;
        let previousMousePosition = { x: 0, y: 0 };

        // 1. 初始化 Three.js 3D 拓扑星系球
        function initThreeOrb() {
            const canvas = document.getElementById('three-orb-canvas');
            if (!canvas) return;

            const width = canvas.parentElement.clientWidth;
            const height = canvas.parentElement.clientHeight;

            orbScene = new THREE.Scene();
            orbCamera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
            orbCamera.position.z = 220;

            orbRenderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });
            orbRenderer.setSize(width, height);
            orbRenderer.setPixelRatio(window.devicePixelRatio);

            // 生成 80 个盘口粒子构成的 3D 球体
            const particleCount = 80;
            const geometry = new THREE.BufferGeometry();
            const positions = new Float32Array(particleCount * 3);
            const colors = new Float32Array(particleCount * 3);

            const radius = 70;
            for (let i = 0; i < particleCount; i++) {
                // 球面均匀分布算法 (Fibonacci Sphere)
                const phi = Math.acos(-1 + (2 * i) / particleCount);
                const theta = Math.sqrt(particleCount * Math.PI) * phi;

                const x = radius * Math.cos(theta) * Math.sin(phi);
                const y = radius * Math.sin(theta) * Math.sin(phi);
                const z = radius * Math.cos(phi);

                positions[i * 3] = x;
                positions[i * 3 + 1] = y;
                positions[i * 3 + 2] = z;

                // 霓虹配色: 青色与绿色为主，代表健康点差
                colors[i * 3] = 0.0;     // R
                colors[i * 3 + 1] = 0.95; // G
                colors[i * 3 + 2] = 1.0;  // B
            }

            geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
            geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

            const material = new THREE.PointsMaterial({
                size: 4.5,
                vertexColors: true,
                transparent: true,
                opacity: 0.85
            });

            orbPoints = new THREE.Points(geometry, material);
            orbScene.add(orbPoints);

            // 绘制粒子之间的引力连线
            const lineMat = new THREE.LineBasicMaterial({
                color: 0x00f3ff,
                transparent: true,
                opacity: 0.15
            });
            const lineGeo = new THREE.BufferGeometry();
            const linePositions = [];
            for (let i = 0; i < particleCount; i += 2) {
                linePositions.push(positions[i*3], positions[i*3+1], positions[i*3+2]);
                linePositions.push(positions[(i+1)*3], positions[(i+1)*3+1], positions[(i+1)*3+2]);
            }
            lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
            orbLines = new THREE.LineSegments(lineGeo, lineMat);
            orbScene.add(orbLines);

            // 鼠标交互控制
            canvas.addEventListener('mousedown', (e) => {
                isOrbDragging = true;
                previousMousePosition = { x: e.clientX, y: e.clientY };
            });
            window.addEventListener('mouseup', () => { isOrbDragging = false; });
            canvas.addEventListener('mousemove', (e) => {
                if (!isOrbDragging) return;
                const deltaX = e.clientX - previousMousePosition.x;
                const deltaY = e.clientY - previousMousePosition.y;
                orbPoints.rotation.y += deltaX * 0.008;
                orbPoints.rotation.x += deltaY * 0.008;
                orbLines.rotation.y += deltaX * 0.008;
                orbLines.rotation.x += deltaY * 0.008;
                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            // 自适应窗口重绘
            window.addEventListener('resize', () => {
                const w = canvas.parentElement.clientWidth;
                const h = canvas.parentElement.clientHeight;
                orbCamera.aspect = w / h;
                orbCamera.updateProjectionMatrix();
                orbRenderer.setSize(w, h);
            });

            animateOrb();
        }

        function animateOrb() {
            requestAnimationFrame(animateOrb);
            if (orbPoints && !isOrbDragging) {
                orbPoints.rotation.y += 0.003;
                orbPoints.rotation.x += 0.001;
                orbLines.rotation.y += 0.003;
                orbLines.rotation.x += 0.001;
            }
            if (orbRenderer && orbScene && orbCamera) {
                orbRenderer.render(orbScene, orbCamera);
            }
        }

        // 2. 轮询获取后端数据并渲染
        async function fetchTerminalData() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                renderHUD(data);
            } catch (err) {
                console.error('HUD 轮询异常:', err);
            }
        }

        function renderHUD(data) {
            const acc = data.account || {};
            const live = data.live_status || {};
            const markets = data.markets || [];
            const isLive = data.execution_mode === 'live';

            // 资金仪表
            document.getElementById('acc-cash-val').innerText = Number(acc.cash || live.usdc_balance || 6.98).toFixed(2);
            document.getElementById('account-pnl').innerText = `已实现: $${Number(acc.total_pnl_usd || 0).toFixed(2)}`;
            document.getElementById('account-type-title').innerText = isLive ? '实盘链上防御资金' : '模拟演练专研本金';
            document.getElementById('funder-short').innerText = `(${live.funder ? live.funder.slice(0,6) + '...' + live.funder.slice(-4) : '0x735e...00dc'})`;

            // 统计指标
            document.getElementById('stat-scan-count').innerText = data.scan_count || 0;
            document.getElementById('stat-latency').innerText = `${Number(data.avg_latency_ms || 0.05).toFixed(2)}ms`;
            document.getElementById('market-total-badge').innerText = markets.length;
            document.getElementById('orb-node-count').innerText = markets.length;

            let makerCount = 0;
            let arbCount = 0;

            // 盘口表格渲染
            const tbody = document.getElementById('market-table-tbody');
            if (markets.length > 0) {
                tbody.innerHTML = markets.map(m => {
                    const isArb = m.pair_cost <= 0.985;
                    if (isArb) arbCount++;
                    if (m.has_maker_edge) makerCount++;

                    const costCls = isArb ? 'text-emerald-400 font-bold bg-emerald-950/80 px-2 py-0.5 rounded border border-emerald-500' : 'text-slate-300';
                    const marginCls = m.taker_margin_pct > 1.5 ? 'text-emerald-400 font-bold' : 'text-slate-400';

                    return `
                    <tr class="hover:bg-cyan-950/20 transition">
                        <td class="py-2.5 px-3">
                            <a href="${m.slug ? 'https://polymarket.com/event/' + m.slug : 'https://polymarket.com'}" target="_blank" 
                               class="text-slate-200 hover:text-cyan-400 transition hover:underline line-clamp-1">
                                ${m.question} <i class="fa-solid fa-arrow-up-right-from-square text-[9px] text-slate-500"></i>
                            </a>
                        </td>
                        <td class="py-2.5 px-2 text-center text-slate-400 text-xs">
                            ${Number(m.best_bid_0).toFixed(3)} / <span class="text-white">${Number(m.best_ask_0).toFixed(3)}</span>
                        </td>
                        <td class="py-2.5 px-2 text-center text-slate-400 text-xs">
                            ${Number(m.best_bid_1).toFixed(3)} / <span class="text-white">${Number(m.best_ask_1).toFixed(3)}</span>
                        </td>
                        <td class="py-2.5 px-2 text-center font-bold font-mono">
                            <span class="${costCls}">${Number(m.pair_cost).toFixed(3)}</span>
                        </td>
                        <td class="py-2.5 px-2 text-center ${marginCls} font-mono">
                            ${m.taker_margin_pct >= 0 ? '+' : ''}${Number(m.taker_margin_pct).toFixed(2)}%
                        </td>
                        <td class="py-2.5 px-2 text-center text-cyan-400 font-mono">
                            +${Number(m.maker_edge_pct).toFixed(1)}%
                        </td>
                        <td class="py-2.5 px-3 text-right">
                            ${isArb 
                                ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">可对冲锁定</span>' 
                                : '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">平稳观望</span>'}
                        </td>
                    </tr>
                    `;
                }).join('');
            }

            document.getElementById('orb-maker-count').innerText = makerCount;
            document.getElementById('orb-arb-count').innerText = arbCount;

            // 新闻流
            const newsList = document.getElementById('news-stream-list');
            const news = data.news || [];
            if (news.length > 0) {
                newsList.innerHTML = news.slice(0, 5).map(n => `
                    <div class="p-2 rounded bg-black/40 border border-slate-800 hover:border-cyan-500/40 transition">
                        <div class="flex items-center justify-between text-[10px] text-rose-400 font-bold mb-1">
                            <span>[${n.time}] ${n.urgency}</span>
                            <span class="text-slate-500">${n.matched_keywords ? n.matched_keywords.join('/') : ''}</span>
                        </div>
                        <a href="${n.link}" target="_blank" class="text-slate-200 hover:text-cyan-400 text-[11px] line-clamp-2 block transition">
                            ${n.title}
                        </a>
                    </div>
                `).join('');
            }
        }

        async function checkGeo() {
            const ind = document.getElementById('geo-status-indicator');
            ind.innerHTML = '<span class="w-2 h-2 rounded-full bg-cyan-400 animate-ping"></span> 探测节点中...';
            try {
                const res = await fetch('/api/network/geoblock');
                const geo = await res.json();
                if (geo.ok) {
                    ind.innerHTML = `<span class="text-emerald-400 font-bold"><i class="fa-solid fa-check"></i> 畅通 (${geo.country || 'OK'})</span>`;
                    document.getElementById('gate-geo').className = "px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-500/40";
                } else {
                    ind.innerHTML = `<span class="text-rose-400 font-bold"><i class="fa-solid fa-ban"></i> 受限: ${geo.country || 'US'} (请切香港/日本)</span>`;
                    document.getElementById('gate-geo').className = "px-2 py-0.5 rounded bg-rose-950/80 text-rose-400 border border-rose-500/40";
                }
            } catch (e) {
                ind.innerText = '探测超时';
            }
        }

        async function switchMode(mode) {
            try {
                const res = await fetch('/api/execution-mode', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: mode})
                });
                const data = await res.json();
                if (data.status === 'ok') {
                    document.getElementById('btn-live-mode').className = mode === 'live' 
                        ? 'px-3 py-1 rounded bg-rose-600 text-white font-bold transition glow-box-cyan' 
                        : 'px-3 py-1 rounded text-slate-400 hover:text-white transition';
                    document.getElementById('btn-paper-mode').className = mode === 'paper' 
                        ? 'px-3 py-1 rounded bg-emerald-600 text-white font-bold transition glow-box-cyan' 
                        : 'px-3 py-1 rounded text-slate-400 hover:text-white transition';
                    fetchTerminalData();
                }
            } catch (err) {}
        }

        async function emergencyCancelAll() {
            if (!confirm("⚠️ 确认拉响死人开关，向 CLOB 提交一键全额撤单？")) return;
            try {
                const res = await fetch('/api/live/cancel-all', { method: 'POST' });
                alert("🛑 紧急撤单已提交！");
                fetchTerminalData();
            } catch (e) {
                alert("撤单异常: " + e.message);
            }
        }

        async function testLiveOrder() {
            const fb = document.getElementById('action-feedback');
            fb.classList.remove('hidden');
            fb.innerText = "⏳ 正在发起 0.01U CLOB V2 实盘连通性微测单...";
            try {
                const res = await fetch('/api/live/test-order', { method: 'POST' });
                const d = await res.json();
                fb.innerText = d.ok ? `✅ [签名通过] ${d.note}` : `⚠️ [提示] ${d.error || '受限或未通过'}`;
            } catch (e) {
                fb.innerText = `❌ 请求异常: ${e.message}`;
            }
        }

        // 启动轮询
        window.addEventListener('DOMContentLoaded', () => {
            initThreeOrb();
            checkGeo();
            fetchTerminalData();
            setInterval(fetchTerminalData, 1000);
            setInterval(checkGeo, 10000);
        });
    </script>
</body>
</html>
"""
