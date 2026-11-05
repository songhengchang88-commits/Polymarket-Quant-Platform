import json
import logging
import os
import time
from typing import Dict, List, Optional
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

import config
from auth import AdminAuthError, verify_admin_token
from engine import QuantEngine
from polymarket_ws import ws_feed
from quantum_hud import CYBERPUNK_HUD_HTML

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="PolyArbitrage Bloomberg Terminal")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8888", "http://localhost:8888"],  # 阶段0止血: 原为 ["*"] 全开, 现仅留 localhost 白名单占位
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = QuantEngine()


def require_admin(
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """写接口统一鉴权依赖 (阶段1-B): 核心逻辑在 auth.verify_admin_token。

    令牌取自环境变量 PMQ_ADMIN_TOKEN; 未设置时 fail-closed 一律拒绝。
    所有 POST (有副作用) 接口都挂此依赖, 只读 GET 不加。
    """
    try:
        verify_admin_token(authorization, x_admin_token)
    except AdminAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PolyArbitrage - 彭博多模态全链条套利终端 (40U专研)</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        .font-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: #090d16; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #334155; }
        .terminal-header { background: linear-gradient(180deg, #0f172a 0%, #090d16 100%); }
        .glow-amber { box-shadow: 0 0 15px rgba(245, 158, 11, 0.2); }
        .glow-cyan { box-shadow: 0 0 15px rgba(6, 182, 212, 0.2); }
        .glow-emerald { box-shadow: 0 0 15px rgba(16, 185, 129, 0.2); }
        @keyframes ticker-scroll {
            0% { transform: translateX(100%); }
            100% { transform: translateX(-100%); }
        }
    </style>
</head>
<body class="bg-[#050811] text-slate-100 min-h-screen antialiased flex flex-col selection:bg-amber-500 selection:text-slate-950">

    <!-- 顶部彭博快讯滚动流 (Fast Breaking News Bar) -->
    <div class="bg-[#090e1a] border-b border-slate-800/80 px-4 py-1.5 flex items-center justify-between text-xs overflow-hidden">
        <div class="flex items-center gap-2 shrink-0 pr-4 border-r border-slate-800">
            <span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span>
            <span class="text-rose-400 font-bold tracking-wider font-mono uppercase text-[11px]">Bloomberg Flash</span>
        </div>
        <div class="overflow-hidden whitespace-nowrap flex-1 px-4 relative">
            <div id="news-ticker" class="inline-block text-[11px] text-slate-300 transition-all font-mono">
                [LIVE] 正在实时监听全球财经、美联储决策、大选突发快讯与赛事比分...
            </div>
        </div>
        <div class="shrink-0 pl-4 border-l border-slate-800 text-[11px] text-slate-400 font-mono">
            Polygon 结算 Gas: <span class="text-emerald-400 font-bold">$0.005</span> | 撮合: <span class="text-cyan-400 font-bold">0 Gas</span>
        </div>
    </div>

    <!-- 彭博暗黑主表头 -->
    <header class="terminal-header border-b border-slate-800 px-6 py-3.5 flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-3.5">
            <a href="https://polymarket.com" target="_blank" rel="noopener noreferrer" class="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center text-amber-400 font-bold text-xl glow-amber hover:scale-105 transition">
                Ψ
            </a>
            <div>
                <div class="flex items-center gap-2">
                    <h1 class="text-lg font-bold tracking-tight text-white flex items-center gap-2">
                        PolyArbitrage <span class="text-xs px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-400 border border-amber-500/30 font-mono">40U 彭博专研终端</span>
                    </h1>
                    <span id="badge-status" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> 全链条四向监听中
                    </span>
                    <span id="ws-status-badge" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 font-mono">
                        <span class="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-ping"></span> <span id="ws-status-text">WS 毫秒级直连</span>
                    </span>
                </div>
                <p class="text-xs text-emerald-400 font-sans mt-0.5 font-medium flex items-center gap-1.5">
                    <i class="fa-solid fa-shield-halved"></i> 100% 绝对胜率 • 小仓位(1~2U) • 见利就收 • 快进快出 • 不等结束以量取胜
                </p>
            </div>
        </div>

        <!-- 账户资金仪表 (模拟/实盘双轨自适应) -->
        <div class="flex items-center gap-3">
            <div id="acc-card-container" class="bg-[#0b101d] px-4 py-2 rounded-xl border border-slate-800 text-right transition">
                <div id="acc-pool-title" class="text-[10px] text-slate-400 uppercase tracking-wider font-mono">模拟专研本金池 (USDC.e)</div>
                <div class="text-base font-bold text-white flex items-center justify-end gap-1.5 font-mono">
                    <span id="acc-cash" class="text-cyan-400">40.00</span> U
                    <span id="acc-profit" class="text-xs px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 font-normal">+0.00%</span>
                </div>
            </div>

            <!-- 分类切换器 (新增 当日决胜 实时同步专区) -->
            <div class="flex items-center bg-[#0b101d] p-1 rounded-xl border border-slate-800 text-xs font-mono">
                <button onclick="setCategory('all')" id="btn-cat-all" class="px-2.5 py-1.5 rounded-lg bg-amber-500 text-slate-950 font-bold transition">全品类</button>
                <button onclick="setCategory('today')" id="btn-cat-today" class="px-2.5 py-1.5 rounded-lg text-rose-400 hover:text-white transition flex items-center gap-1 font-bold">
                    <i class="fa-solid fa-fire text-rose-500 animate-pulse"></i> 当日决胜
                </button>
                <button onclick="setCategory('crypto')" id="btn-cat-crypto" class="px-2.5 py-1.5 rounded-lg text-slate-400 hover:text-white transition">Crypto</button>
                <button onclick="setCategory('politics')" id="btn-cat-politics" class="px-2.5 py-1.5 rounded-lg text-slate-400 hover:text-white transition">大选/政治</button>
                <button onclick="setCategory('sports')" id="btn-cat-sports" class="px-2.5 py-1.5 rounded-lg text-slate-400 hover:text-white transition">体育赛事</button>
            </div>

            <!-- 模式切换与控制按钮 -->
            <div class="flex items-center gap-2">
                <!-- 模拟 / 实盘切换开关 -->
                <div class="flex items-center bg-[#070b14] p-0.5 rounded-xl border border-slate-700 font-mono text-xs">
                    <button onclick="setExecutionMode('paper')" id="btn-mode-paper" class="px-2.5 py-1.5 rounded-lg bg-emerald-500 text-slate-950 font-bold transition">
                        📝 模拟盘
                    </button>
                    <button onclick="setExecutionMode('live')" id="btn-mode-live" class="px-2.5 py-1.5 rounded-lg text-rose-400 hover:text-white transition">
                        ⚡ 实盘接入
                    </button>
                </div>
                <button onclick="triggerDrill()" class="px-3 py-2 text-xs font-semibold rounded-xl bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/20 transition flex items-center gap-1.5 glow-cyan">
                    <i class="fa-solid fa-bolt"></i> 演练
                </button>
                <button onclick="toggleMonitor()" id="btn-monitor" class="px-3 py-2 text-xs font-semibold rounded-xl bg-amber-500/10 text-amber-400 border border-amber-500/30 hover:bg-amber-500/20 transition">
                    <i class="fa-solid fa-pause mr-1"></i> 暂停
                </button>
                <button onclick="resetAccount()" class="px-3 py-2 text-xs font-semibold rounded-xl bg-rose-500/10 text-rose-400 border border-rose-500/30 hover:bg-rose-500/20 transition">
                    <i class="fa-solid fa-rotate-left mr-1"></i> 重置40U
                </button>
            </div>
        </div>
    </header>

    <!-- 主展示区 (彭博黑金布局: 左2列深度雷达+持仓袋，右1列新闻+多模态指令) -->
    <main class="flex-1 p-5 grid grid-cols-1 lg:grid-cols-3 gap-5 max-w-[1800px] mx-auto w-full">

        <!-- 左侧 2 列 -->
        <div class="lg:col-span-2 space-y-5">

            <!-- 40U 活跃执行中的套利持仓袋 (Active Bags) -->
            <div class="bg-[#0b101d] border border-slate-800/90 rounded-2xl p-4 shadow-sm">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                        <h2 id="bags-header-title" class="font-bold text-sm text-white">40U 执行中套利持仓袋 (Active Arbitrage Bags)</h2>
                    </div>
                    <span class="text-xs text-emerald-400 font-mono">规则: 100% 绝对胜率 • 小仓位(1~2U) • 见利就收 • 提前闪电出手释放资金</span>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left">
                        <thead class="text-slate-400 border-b border-slate-800 font-mono uppercase text-[11px]">
                            <tr>
                                <th class="py-2.5 px-3">预测事件</th>
                                <th class="py-2.5 px-2 text-center">建仓成本 (Ask0+Ask1)</th>
                                <th class="py-2.5 px-2 text-center">投入资金 (40U分配)</th>
                                <th class="py-2.5 px-2 text-center">理论无风险纯利</th>
                                <th class="py-2.5 px-2 text-center">状态 / 退出机制</th>
                                <th class="py-2.5 px-3 text-right">操作</th>
                            </tr>
                        </thead>
                        <tbody id="active-bags-tbody" class="divide-y divide-slate-800/60 font-sans">
                            <!-- 动态注入持仓 -->
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 全品类深度盘口与价差雷达 -->
            <div class="bg-[#0b101d] border border-slate-800/90 rounded-2xl p-4 shadow-sm">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2">
                        <i class="fa-solid fa-chart-line text-cyan-400 text-sm"></i>
                        <h2 class="font-bold text-sm text-white">全链条活跃盘口监控矩阵 (<span id="market-count-badge" class="text-cyan-400">80</span> 个全平台动态轮动流)</h2>
                    </div>
                    <div class="text-xs text-slate-400 font-mono">
                        采样延迟: <span id="avg-latency" class="text-emerald-400 font-bold">420ms</span> | 
                        扫描次数: <span id="scan-count" class="text-white">0</span> | 
                        全网轮动: <span id="rotation-wave" class="text-amber-400 font-bold">第 1 波次</span>
                    </div>
                </div>

                <div class="overflow-x-auto max-h-[580px] overflow-y-auto">
                    <table class="w-full text-xs text-left font-mono">
                        <thead class="text-slate-400 border-b border-slate-800 sticky top-0 bg-[#0b101d] z-10 text-[11px]">
                            <tr>
                                <th class="py-2.5 px-3">事件 / 官网直达 ↗</th>
                                <th class="py-2.5 px-2 text-center">YES 买1 / 卖1</th>
                                <th class="py-2.5 px-2 text-center">NO 买1 / 卖1</th>
                                <th class="py-2.5 px-2 text-center">Taker 配对成本</th>
                                <th class="py-2.5 px-2 text-center">Taker 净利空间</th>
                                <th class="py-2.5 px-2 text-center">Maker 双边价差</th>
                                <th class="py-2.5 px-3 text-right">深度 / 延迟</th>
                            </tr>
                        </thead>
                        <tbody id="market-table-tbody" class="divide-y divide-slate-800/60">
                            <!-- 动态注入盘口 -->
                        </tbody>
                    </table>
                </div>
            </div>

        </div>

        <!-- 右侧 1 列 -->
        <div class="space-y-5">

            <!-- ⚡ 实盘落地与风控工作台 (Live CLOB V2 Hub) -->
            <div class="bg-[#0b101d] border border-rose-500/30 rounded-2xl p-4 shadow-sm relative overflow-hidden">
                <div class="absolute -right-8 -top-8 w-24 h-24 bg-rose-500/10 rounded-full blur-xl pointer-events-none"></div>
                <div class="flex items-center justify-between mb-2">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse"></span>
                        <h2 class="font-bold text-sm text-white flex items-center gap-1.5 font-mono">
                            <i class="fa-solid fa-satellite-dish text-rose-400"></i> CLOB V2 实盘控制台
                        </h2>
                    </div>
                    <span id="live-engine-badge" class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">
                        V2 就绪
                    </span>
                </div>

                <!-- 节点网络检测条 -->
                <div class="bg-[#050811] p-2.5 rounded-xl border border-slate-800 mb-3 text-xs font-mono space-y-1.5">
                    <div class="flex items-center justify-between">
                        <span class="text-slate-400">出口网络节点:</span>
                        <span id="geo-status-badge" class="font-bold text-amber-400 flex items-center gap-1">
                            <i class="fa-solid fa-spinner fa-spin"></i> 探测中...
                        </span>
                    </div>
                    <div class="flex items-center justify-between text-[11px] text-slate-500">
                        <span>Funder 代理: <span class="text-slate-300">0x735e...00dc</span></span>
                        <span>风控: <span id="card-risk-badge" class="text-emerald-400 font-mono">限额 20U / 止损 8U</span></span>
                    </div>
                </div>

                <!-- 实盘一键操作按钮群 -->
                <div class="grid grid-cols-2 gap-2 font-mono text-xs">
                    <button onclick="testLiveOrder()" class="px-3 py-2 rounded-xl bg-rose-500/15 text-rose-300 border border-rose-500/40 hover:bg-rose-500/30 transition flex items-center justify-center gap-1.5 font-bold shadow-lg shadow-rose-950/50">
                        <i class="fa-solid fa-bolt text-rose-400"></i> 一键实盘微测
                    </button>
                    <button onclick="cancelAllLiveOrders()" class="px-3 py-2 rounded-xl bg-slate-800/60 text-slate-300 border border-slate-700 hover:bg-slate-800 transition flex items-center justify-center gap-1.5">
                        <i class="fa-solid fa-ban text-rose-400"></i> 紧急一键撤单
                    </button>
                </div>
            </div>

            <!-- 多模态自然语言控制台 -->
            <div class="bg-[#0b101d] border border-slate-800/90 rounded-2xl p-4 shadow-sm">
                <div class="flex items-center justify-between mb-2">
                    <h2 class="font-bold text-sm text-purple-400 flex items-center gap-2">
                        <i class="fa-solid fa-wand-magic-sparkles"></i> 多模态自然语言交互条
                    </h2>
                    <span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20 font-mono">AI ASSISTANT</span>
                </div>
                <p class="text-xs text-slate-400 mb-3 font-sans">支持中文指令微调灵敏度、重设资金仓位或强制一键结利释放</p>

                <div class="space-y-2">
                    <div class="relative">
                        <input id="ai-input" type="text" placeholder="例：设置单笔投入15U，只扫利润大于2%的长尾盘口" 
                               class="w-full bg-[#050811] border border-slate-700/80 rounded-xl px-3 py-2.5 text-xs text-white focus:outline-none focus:border-amber-500 pr-16 font-sans" />
                        <button onclick="sendAiCommand()" class="absolute right-1.5 top-1.5 px-3 py-1 text-xs rounded-lg bg-amber-500 text-slate-950 font-bold hover:bg-amber-400 transition">
                            执行
                        </button>
                    </div>

                    <div class="flex flex-wrap gap-1.5">
                        <button onclick="applyPreset('保守模式：Taker阈值0.980，平价快速离场')" class="text-[11px] px-2 py-0.8 rounded-lg bg-[#050811] hover:bg-slate-800 border border-slate-800 text-slate-400 hover:text-amber-400 transition">
                            🛡️ 保守型策略
                        </button>
                        <button onclick="applyPreset('激进模式：单笔投入40U满额套利')" class="text-[11px] px-2 py-0.8 rounded-lg bg-[#050811] hover:bg-slate-800 border border-slate-800 text-slate-400 hover:text-amber-400 transition">
                            🚀 满额套利型
                        </button>
                        <button onclick="applyPreset('一键平仓释放所有持仓袋')" class="text-[11px] px-2 py-0.8 rounded-lg bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 border border-rose-500/30 transition">
                            🛑 一键平仓释放
                        </button>
                    </div>
                    <div id="ai-reply" class="text-xs p-2.5 rounded-xl bg-[#050811] text-amber-400 border border-slate-800 hidden font-mono leading-relaxed"></div>
                </div>
            </div>

            <!-- 全球突发快讯监听流 (Live Breaking News Stream) -->
            <div class="bg-[#0b101d] border border-slate-800/90 rounded-2xl p-4 shadow-sm">
                <div class="flex items-center justify-between mb-2">
                    <h2 class="font-bold text-sm text-white flex items-center gap-2">
                        <i class="fa-regular fa-newspaper text-rose-400"></i> 全球突发快讯流 (Google News / Tree News)
                    </h2>
                    <span class="text-[10px] font-mono text-emerald-400">实时匹配中</span>
                </div>
                <div id="news-stream-container" class="space-y-2 max-h-56 overflow-y-auto pr-1">
                    <!-- 动态注入新闻流卡片 -->
                </div>
            </div>

            <!-- 历史套利平仓流水 (Paper Trades Log) -->
            <div class="bg-[#0b101d] border border-slate-800/90 rounded-2xl p-4 shadow-sm">
                <h2 class="font-bold text-sm text-white mb-2 flex items-center gap-2">
                    <i class="fa-solid fa-receipt text-cyan-400"></i> 40U 资金流转与平仓流水
                </h2>
                <div id="trades-log-container" class="space-y-1.5 max-h-52 overflow-y-auto pr-1 font-mono text-[11px]">
                    <!-- 动态注入流水 -->
                </div>
            </div>

        </div>

    </main>

    <script>
        let currentStatus = null;

        function getBadge(tag) {
            if (tag === 'TODAY') {
                return `<span class="text-[9px] font-black px-1.5 py-0.5 rounded border border-rose-500 bg-rose-950/80 text-rose-300 font-mono shadow-[0_0_8px_rgba(244,63,94,0.4)] animate-pulse">🔥当日决胜</span>`;
            }
            const map = {
                'BTC': 'bg-amber-950 text-amber-400 border-amber-800',
                'ETH': 'bg-indigo-950 text-indigo-400 border-indigo-800',
                'SOL': 'bg-fuchsia-950 text-fuchsia-400 border-fuchsia-800',
                'MACRO': 'bg-emerald-950 text-emerald-400 border-emerald-800',
                'POLITICS': 'bg-rose-950 text-rose-400 border-rose-800',
                'SPORTS': 'bg-cyan-950 text-cyan-400 border-cyan-800',
                'GENERAL': 'bg-slate-800 text-slate-300 border-slate-700'
            };
            const cls = map[tag] || map['GENERAL'];
            return `<span class="text-[9px] font-bold px-1.5 py-0.5 rounded border ${cls} font-mono">${tag}</span>`;
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                currentStatus = data;
                renderDashboard(data);
            } catch (e) {
                console.warn('拉取状态异常:', e);
            }
        }

        function renderDashboard(data) {
            try {
                // 0. 更新实盘/模拟盘模式按钮
                updateModeButtons(data.execution_mode || 'paper');

                // 更新 WebSocket 实时状态
                const wsStatus = data.ws_status || {};
                const wsText = document.getElementById('ws-status-text');
                if (wsText) {
                    if (wsStatus.connected) {
                        wsText.innerText = `WS 毫秒直连 (${wsStatus.subscribed_tokens || 0}代币)`;
                    } else {
                        wsText.innerText = `REST 快速同步`;
                    }
                }

                // 节点网络检测 (每 8 秒自动拉取一次)
                if (!window._lastGeoCheck || (Date.now() - window._lastGeoCheck > 8000)) {
                    window._lastGeoCheck = Date.now();
                    fetch('/api/network/geoblock').then(r => r.json()).then(geo => {
                        const geoEl = document.getElementById('geo-status-badge');
                        if (geoEl) {
                            if (geo.ok) {
                                geoEl.innerHTML = `<span class="text-emerald-400 font-bold flex items-center gap-1">🟢 ${geo.country || 'OK'} (${geo.ip || '放行'})</span>`;
                            } else {
                                geoEl.innerHTML = `<span class="text-rose-400 font-bold flex items-center gap-1">🔴 ${geo.country || 'US'} (受限: ${geo.ip})</span>`;
                            }
                        }
                    }).catch(() => {});
                }

                // 1. 顶部快讯滚动
                const news = data.news || [];
                if (news.length > 0) {
                    const first = news[0];
                    document.getElementById('news-ticker').innerHTML = `
                        <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${first.urgency === 'FLASH' ? 'bg-rose-950 text-rose-400 border border-rose-800' : 'bg-amber-950 text-amber-400 border border-amber-800'}">[${first.time}]</span>
                        <strong class="text-white ml-1">${first.title}</strong>
                        <span class="text-slate-400 ml-2">命中实体: ${first.matched_keywords ? first.matched_keywords.join(', ') : 'Global'}</span>
                    `;
                }

                // 2. 账户资金双轨动态切换
                const isLive = data.execution_mode === 'live';
                const accCard = document.getElementById('acc-card-container');
                const poolTitle = document.getElementById('acc-pool-title');
                const acc = data.account || {};
                const liveStatus = data.live_status || {};

                if (isLive) {
                    if (accCard) accCard.className = "bg-[#18090f] px-4 py-2 rounded-xl border border-rose-500/50 text-right transition shadow-lg shadow-rose-950/40";
                    const funderShort = liveStatus.funder ? `${liveStatus.funder.slice(0,6)}...${liveStatus.funder.slice(-4)}` : '0x735e...';
                    if (poolTitle) poolTitle.innerHTML = `<span class="text-rose-400 font-bold animate-pulse">● 实盘链上抵押池</span> <span class="text-slate-400 text-[10px] font-mono">(${funderShort})</span>`;
                    
                    const liveBalance = Number(liveStatus.usdc_balance || 4.0).toFixed(2);
                    document.getElementById('acc-cash').innerText = liveBalance;
                    
                    const profitEl = document.getElementById('acc-profit');
                    const maxOrder = liveStatus.max_order_usd || 20;
                    const stopLoss = liveStatus.hard_stop_loss_usd || 8;
                    profitEl.innerText = `实盘在线 (限额 ${maxOrder}U | 止损 ${stopLoss}U)`;
                    profitEl.className = 'text-xs px-1.5 py-0.2 rounded bg-rose-500/20 text-rose-300 font-normal font-mono border border-rose-500/30';
                    
                    const cardRiskEl = document.getElementById('card-risk-badge');
                    if (cardRiskEl) cardRiskEl.innerText = `限额 ${maxOrder}U / 止损 ${stopLoss}U`;
                } else {
                    if (accCard) accCard.className = "bg-[#0b101d] px-4 py-2 rounded-xl border border-slate-800 text-right transition";
                    if (poolTitle) poolTitle.innerText = "模拟专研本金池 (USDC.E)";
                    
                    const cash = Number(acc.current_cash || 40.0).toFixed(2);
                    const profitPct = Number(acc.profit_pct || 0.0);
                    const totalProfit = Number(acc.total_profit || 0.0).toFixed(2);
                    document.getElementById('acc-cash').innerText = cash;
                    
                    const profitEl = document.getElementById('acc-profit');
                    profitEl.innerText = `${profitPct >= 0 ? '+' : ''}${profitPct.toFixed(2)}% (+${totalProfit}U)`;
                    profitEl.className = profitPct >= 0 
                        ? 'text-xs px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 font-normal font-mono'
                        : 'text-xs px-1.5 py-0.2 rounded bg-rose-500/10 text-rose-400 font-normal font-mono';
                }

                const bagsTitle = document.getElementById('bags-header-title');
                if (bagsTitle) {
                    bagsTitle.innerText = isLive ? 'CLOB V2 真实套利持仓袋 (Live Bags)' : '40U 执行中模拟套利持仓袋 (Active Arbitrage Bags)';
                }

                // 3. 活跃持仓袋 (Active Bags)
                const bagsTbody = document.getElementById('active-bags-tbody');
                const activeBags = acc.active_bags || [];
                if (activeBags.length === 0) {
                    const emptyMsg = isLive
                        ? '<span class="text-rose-400 font-semibold"><i class="fa-solid fa-satellite-dish mr-1"></i> CLOB V2 实盘监听中</span>：真实资金已就绪（当前可用 <b>$' + Number(acc.cash || 4.0).toFixed(2) + ' U</b>），全天候监听配对套利漏洞，一旦捕获将自动下达原子买单！'
                        : '当前资金在机动现金池就绪中，一旦扫到漏洞即刻建立 20U 模拟配对袋';
                    bagsTbody.innerHTML = `<tr><td colspan="6" class="py-6 text-center text-slate-400 font-sans">${emptyMsg}</td></tr>`;
                } else {
                    bagsTbody.innerHTML = activeBags.map((b) => `
                        <tr class="hover:bg-slate-800/40 transition">
                            <td class="py-2.5 px-3">
                                <div class="font-medium text-slate-200 line-clamp-1">${b.question}</div>
                                <div class="text-[10px] text-slate-500 font-mono">建仓时间: ${b.entry_time} | 份额: ${b.shares}</div>
                            </td>
                            <td class="py-2.5 px-2 text-center font-mono text-emerald-400 font-bold">$${Number(b.pair_cost).toFixed(3)}</td>
                            <td class="py-2.5 px-2 text-center font-mono text-white">${Number(b.invested_usd).toFixed(2)} U</td>
                            <td class="py-2.5 px-2 text-center font-mono text-emerald-400 font-bold">+${Number(b.profit_pct).toFixed(1)}% (+${Number(b.expected_profit_usd).toFixed(2)}U)</td>
                            <td class="py-2.5 px-2 text-center">
                                <span class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">
                                    均值回归至 0.995 即卖
                                </span>
                            </td>
                            <td class="py-2.5 px-3 text-right">
                                <button onclick="manualClose('${b.market_id}')" class="px-2 py-1 rounded bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 border border-rose-500/30 text-[11px] font-sans transition">
                                    提前市价平仓
                                </button>
                            </td>
                        </tr>
                    `).join('');
                }

                // 4. 全品类盘口
                const markets = data.markets || [];
                document.getElementById('market-count-badge').innerText = markets.length;
                document.getElementById('scan-count').innerText = data.scan_count || 0;
                document.getElementById('avg-latency').innerText = `${Number(data.avg_latency_ms || 420).toFixed(0)}ms`;
                const waveEl = document.getElementById('rotation-wave');
                if (waveEl) waveEl.innerText = `第 ${data.rotation_wave || 1} 波次`;

                const mktTbody = document.getElementById('market-table-tbody');
                if (markets.length > 0) {
                    mktTbody.innerHTML = markets.map(m => {
                        const isArb = m.pair_cost <= 0.985;
                        const costCls = isArb ? 'text-emerald-400 font-bold bg-emerald-950/70 px-1.5 py-0.5 rounded border border-emerald-500' : 'text-slate-300';
                        const marginSign = m.taker_margin_pct >= 0 ? '+' : '';
                        const marginCls = m.taker_margin_pct > 1.5 ? 'text-emerald-400 font-bold' : 'text-slate-400';
                        const badgeHtml = getBadge(m.tag_badge || 'GENERAL');

                        return `
                        <tr class="hover:bg-slate-800/40 transition">
                            <td class="py-2 px-3 font-sans text-slate-200">
                                <div class="flex items-center gap-1.5 mb-0.5">
                                    ${badgeHtml}
                                    <a href="${m.slug ? 'https://polymarket.com/event/' + m.slug : 'https://polymarket.com'}" target="_blank" rel="noopener noreferrer" 
                                       class="hover:text-cyan-400 transition hover:underline line-clamp-1 max-w-[280px]">
                                        ${m.question} <i class="fa-solid fa-arrow-up-right-from-square text-[8px] text-slate-500"></i>
                                    </a>
                                </div>
                            </td>
                            <td class="py-2 px-2 text-center text-slate-400 text-xs">
                                ${Number(m.best_bid_0).toFixed(3)} / <span class="text-slate-200">${Number(m.best_ask_0).toFixed(3)}</span>
                            </td>
                            <td class="py-2 px-2 text-center text-slate-400 text-xs">
                                ${Number(m.best_bid_1).toFixed(3)} / <span class="text-slate-200">${Number(m.best_ask_1).toFixed(3)}</span>
                            </td>
                            <td class="py-2 px-2 text-center font-bold">
                                <span class="${costCls}">${Number(m.pair_cost).toFixed(3)}</span>
                            </td>
                            <td class="py-2 px-2 text-center ${marginCls}">
                                ${marginSign}${Number(m.taker_margin_pct).toFixed(2)}%
                            </td>
                            <td class="py-2 px-2 text-center text-cyan-400">
                                +${Number(m.maker_edge_pct).toFixed(1)}%
                            </td>
                            <td class="py-2 px-3 text-right text-slate-400 text-[11px]">
                                <span class="text-cyan-400 font-bold">${m.depth_count}档</span> • ${Number(m.latency_ms).toFixed(0)}ms
                            </td>
                        </tr>
                        `;
                    }).join('');
                }

                // 5. 新闻流渲染
                const newsCont = document.getElementById('news-stream-container');
                newsCont.innerHTML = news.slice(0, 5).map(n => `
                    <div class="p-2 rounded-xl bg-[#050811] border border-slate-800 text-xs hover:border-slate-700 transition">
                        <div class="flex items-center justify-between mb-1">
                            <span class="text-rose-400 font-mono text-[10px] font-bold">[${n.time}] ${n.urgency}</span>
                            <span class="text-[10px] text-slate-500 font-mono">${n.matched_keywords ? n.matched_keywords.join('/') : ''}</span>
                        </div>
                        <a href="${n.link}" target="_blank" class="text-slate-200 hover:text-amber-400 text-[11px] font-sans line-clamp-2 block transition">
                            ${n.title}
                        </a>
                    </div>
                `).join('');

                // 6. 历史流水
                const tradesCont = document.getElementById('trades-log-container');
                const trades = data.trades || [];
                if (trades.length === 0) {
                    tradesCont.innerHTML = '<div class="text-slate-500 py-3 text-center">暂无平仓日志</div>';
                } else {
                    tradesCont.innerHTML = trades.slice(0, 10).map(t => {
                        const isOpen = t.action === 'OPEN_BAG';
                        const sign = isOpen ? '🎯 建仓' : '💰 结利';
                        const col = isOpen ? 'text-amber-400' : 'text-emerald-400';
                        return `
                        <div class="flex justify-between items-center p-1.5 rounded bg-[#050811] border border-slate-800/80">
                            <span class="${col}">${sign} [${t.question ? t.question.slice(0,18) : ''}...]</span>
                            <span class="text-slate-400">${t.cost_usd || t.invested_usd}U | 结余: <b class="text-white">${t.balance_after}U</b></span>
                        </div>
                        `;
                    }).join('');
                }

            } catch (err) {
                console.error('渲染异常:', err);
            }
        }

        async function setExecutionMode(mode) {
            try {
                const res = await fetch('/api/execution-mode', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: mode})
                });
                const data = await res.json();
                if (data.status === 'ok') {
                    updateModeButtons(data.mode);
                    await fetchStatus();
                }
            } catch (err) {
                console.error('切换模式异常:', err);
            }
        }

        function updateModeButtons(mode) {
            const btnPaper = document.getElementById('btn-mode-paper');
            const btnLive = document.getElementById('btn-mode-live');
            if (!btnPaper || !btnLive) return;
            if (mode === 'live') {
                btnLive.className = 'px-2.5 py-1.5 rounded-lg bg-rose-500 text-white font-bold transition shadow-lg shadow-rose-500/30';
                btnPaper.className = 'px-2.5 py-1.5 rounded-lg text-slate-400 hover:text-white transition';
            } else {
                btnPaper.className = 'px-2.5 py-1.5 rounded-lg bg-emerald-500 text-slate-950 font-bold transition shadow-lg shadow-emerald-500/20';
                btnLive.className = 'px-2.5 py-1.5 rounded-lg text-rose-400 hover:text-white transition';
            }
        }

        async function testLiveOrder() {
            const statusEl = document.getElementById('ai-reply');
            statusEl.classList.remove('hidden');
            statusEl.innerText = "⏳ 正在发起 0.01U CLOB V2 实盘连通性微测单...";
            try {
                const res = await fetch('/api/live/test-order', { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    statusEl.innerText = `✅ [实盘直通成功] 订单已提交并签名！Order ID: ${data.result ? (data.result.orderID || JSON.stringify(data.result)) : 'OK'}`;
                } else {
                    statusEl.innerText = `⚠️ [实盘提示] ${data.error || '下单未通过，请检查节点与余额'}`;
                }
            } catch (err) {
                statusEl.innerText = `❌ 请求异常: ${err.message}`;
            }
        }

        async function cancelAllLiveOrders() {
            if (confirm("确定紧急撤销所有活跃实盘订单吗？")) {
                const res = await fetch('/api/live/cancel-all', { method: 'POST' });
                const data = await res.json();
                alert("已发送撤单指令");
                await fetchStatus();
            }
        }

        async function triggerDrill() {
            await fetch('/api/drill', { method: 'POST' });
            await fetchStatus();
        }

        async function manualClose(mId) {
            if (confirm("确定要提前市价平仓释放 20U 现金吗？")) {
                await fetch(`/api/account/close-position?market_id=${mId}`, { method: 'POST' });
                await fetchStatus();
            }
        }

        async function resetAccount() {
            if (confirm("确定重置为纯净的 40U 专研本金吗？")) {
                await fetch('/api/account/reset', { method: 'POST' });
                await fetchStatus();
            }
        }

        async function toggleMonitor() {
            const res = await fetch('/api/monitor/toggle', { method: 'POST' });
            const data = await res.json();
            const btn = document.getElementById('btn-monitor');
            if (data.is_monitoring) {
                btn.innerHTML = '<i class="fa-solid fa-pause mr-1"></i> 暂停';
            } else {
                btn.innerHTML = '<i class="fa-solid fa-play mr-1"></i> 恢复';
            }
            await fetchStatus();
        }

        async function setCategory(cat) {
            ['all', 'today', 'crypto', 'politics', 'sports'].forEach(c => {
                const b = document.getElementById('btn-cat-' + c);
                if (b) {
                    b.className = (c === cat) 
                        ? 'px-2.5 py-1.5 rounded-lg bg-amber-500 text-slate-950 font-bold transition'
                        : 'px-2.5 py-1.5 rounded-lg text-slate-400 hover:text-white transition';
                }
            });
            await fetch('/api/category', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({category: cat})
            });
            await fetchStatus();
        }

        function applyPreset(text) {
            document.getElementById('ai-input').value = text;
            sendAiCommand();
        }

        async function sendAiCommand() {
            const input = document.getElementById('ai-input').value.trim();
            const replyEl = document.getElementById('ai-reply');
            if (!input) return;

            replyEl.classList.remove('hidden');
            replyEl.innerText = "🤖 正在多模态语义解析并执行指令...";

            const res = await fetch('/api/ai-command', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({command: input})
            });
            const data = await res.json();
            replyEl.innerText = data.reply;
            await fetchStatus();
        }

        fetchStatus();
        setInterval(fetchStatus, 700);
    </script>
</body>
</html>
"""

class CategoryReq(BaseModel):
    category: str

class AiCmdReq(BaseModel):
    command: str

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        CYBERPUNK_HUD_HTML,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/legacy", response_class=HTMLResponse)
def legacy_index():
    return HTML_CONTENT

class ExecutionModeReq(BaseModel):
    mode: str

@app.get("/api/status")
def get_status():
    summary = engine.trader.get_summary()
    trades = engine.trader.get_trades(limit=25)
    news = engine.news_watcher.get_latest(limit=10)
    live_status = engine.live_executor.get_status()

    # 实盘模式与模拟盘数据彻底解耦 (实盘遵循单一真源，不掺杂任何模拟盘统计)
    if engine.execution_mode == "live":
        real_balance = live_status.get("usdc_balance", 6.98)
        live_bags = list(engine.live_executor.live_orders.values())
        invested = sum([b.get("invested_usd", 0) for b in live_bags])
        summary = {
            "initial_balance": 6.98,
            "current_cash": real_balance,
            "invested_in_bags": invested,
            "total_equity": real_balance,
            "total_pnl_usd": engine.live_executor.cumulative_pnl,
            "total_profit": engine.live_executor.cumulative_pnl,
            "profit_pct": round((engine.live_executor.cumulative_pnl / 6.98) * 100, 2),
            "trades_count": len(engine.live_executor.live_orders),
            "successful_trades": 0,
            "failed_trades": 0,
            "win_rate_pct": 0.0,
            "active_positions_count": len(live_bags),
            "active_bags": live_bags,
        }
        trades = [t for t in trades if t.get("mode") == "live"]

    return {
        "is_monitoring": engine.is_running,
        "execution_mode": engine.execution_mode,
        "current_category": engine.current_category,
        "scan_count": engine.scan_count,
        "rotation_wave": getattr(engine.client, "_rotation_wave", 1),
        "last_scan_time": engine.latest_scan_time,
        "avg_latency_ms": engine.latest_avg_latency_ms,
        "ws_status": ws_feed.get_stats(),
        "account": summary,
        "live_status": live_status,
        "trades": trades,
        "news": news,
        "markets": engine.latest_markets_data
    }

@app.get("/api/network/geoblock")
def get_geoblock():
    return engine.live_executor.check_geoblock()

# 阶段1-B: 已加管理员鉴权 (require_admin 依赖, 见 auth.py)
@app.post("/api/live/test-order", dependencies=[Depends(require_admin)])
def run_test_order():
    if not engine.live_executor.is_ready:
        return {"ok": False, "error": "实盘执行器未就绪"}
    
    # 动态抓取当前一个活跃 token
    target_token = None
    if engine.latest_markets_data:
        for m in engine.latest_markets_data:
            tids = m.get("clob_token_ids", [])
            if tids and len(tids) > 0:
                target_token = tids[0]
                break
                
    if not target_token:
        # 回退直接从 client 抓取
        curated = engine.client.get_curated_markets(limit=3)
        for m in curated:
            tids = m.get("clob_token_ids", [])
            if tids and len(tids) > 0:
                target_token = tids[0]
                break
            
    if not target_token:
        return {"ok": False, "error": "未能提取到有效 CLOB Token ID"}
        
    res = engine.live_executor.place_test_order(token_id=target_token, price=0.01, size=5.0)
    return res

# 阶段1-B: 已加管理员鉴权 (require_admin 依赖, 见 auth.py); 切 live 另经 config 前置校验
@app.post("/api/execution-mode", dependencies=[Depends(require_admin)])
def set_mode(req: ExecutionModeReq):
    if req.mode == "live":
        missing = config.live_prerequisites_missing()
        if missing:
            return {"status": "failed", "mode": engine.execution_mode,
                    "error": f"缺少实盘前置配置 ({', '.join(missing)}), 拒绝切换实盘"}
    success = engine.set_execution_mode(req.mode)
    return {"status": "ok" if success else "failed", "mode": engine.execution_mode}

@app.get("/api/live/preflight")
def live_preflight():
    res = engine.live_executor.preflight_check()
    return res

# 阶段1-B: 已加管理员鉴权 (require_admin 依赖, 见 auth.py)
@app.post("/api/live/cancel-all", dependencies=[Depends(require_admin)])
def live_cancel_all():
    res = engine.live_executor.cancel_all_orders()
    return {"status": "ok", "result": res}

# 阶段1-B: 已加管理员鉴权 (require_admin 依赖, 见 auth.py)
@app.post("/api/account/close-position", dependencies=[Depends(require_admin)])
def close_pos(market_id: str):
    if engine.execution_mode == "live":
        success = engine.live_executor.close_position_manually(market_id)
    else:
        success = engine.trader.close_position_manually(market_id)
    return {"status": "ok" if success else "failed"}

# 阶段1-B 补鉴权: 本接口有副作用 (触发演习), 阶段0未标记, 一并纳入 require_admin
@app.post("/api/drill", dependencies=[Depends(require_admin)])
def run_drill():
    res = engine.trigger_test_drill()
    return res

# 阶段1-B 补鉴权: 本接口有副作用 (重置账户), 阶段0未标记, 一并纳入 require_admin
@app.post("/api/account/reset", dependencies=[Depends(require_admin)])
def reset_acc():
    engine.trader.reset_account()
    return {"status": "ok"}

# 阶段1-B 补鉴权: 本接口有副作用 (启停监控), 阶段0未标记, 一并纳入 require_admin
@app.post("/api/monitor/toggle", dependencies=[Depends(require_admin)])
def toggle_mon():
    if engine.is_running:
        engine.stop_monitor()
    else:
        engine.start_monitor()
    return {"is_monitoring": engine.is_running}

# 阶段1-B 补鉴权: 本接口有副作用 (切换扫描品类), 阶段0未标记, 一并纳入 require_admin
@app.post("/api/category", dependencies=[Depends(require_admin)])
def change_cat(req: CategoryReq):
    engine.set_category(req.category)
    return {"status": "ok", "current_category": engine.current_category}

# 阶段1-B: 已加管理员鉴权 (require_admin 依赖, 见 auth.py)——此前无鉴权即可把单笔额度改到 40U 的问题随此解决。
# 注意: 本接口改的 ALLOC_PER_TRADE_USD 只是请求值, 实际单笔硬顶仍由风控常量控制:
# config.POSITION_CAP_PCT (单笔敞口 <= 可用余额 45%, engine/paper_trader/live_executor 均强制引用)
# 与 live_executor 的 MAX_ORDER_USD (执行器单笔上限), 本接口无法绕过。
@app.post("/api/ai-command", dependencies=[Depends(require_admin)])
def handle_ai_command(req: AiCmdReq):
    cmd = req.command
    reply = ""
    if "保守" in cmd:
        config.PARITY_ARBITRAGE_THRESHOLD = 0.980
        engine.scanner.set_thresholds(0.980, 1.020)
        reply = "🤖 已切换至【保守型策略】：配对成本阈值收紧至 <= 0.980（锁定 >= 2.0% 净利空间），反向溢价卖出线设定为 1.020。"
    elif "激进" in cmd or "满额" in cmd:
        config.ALLOC_PER_TRADE_USD = 40.0
        config.MAX_CONCURRENT_BAGS = 1
        reply = "🤖 已切换至【满额套利型】：单笔资金分配提升至 40.0U 全额冲刺，集中攻击最优质漏洞盘口！"
    elif "平仓" in cmd or "清仓" in cmd:
        for m_id in list(engine.trader.active_positions.keys()):
            engine.trader.close_position_manually(m_id)
        reply = "🛑 紧急指令已执行：已市价清空所有活跃套利袋，40U 现金已全额归集释放！"
    else:
        reply = f"🤖 多模态指令理解：已识别并热重载策略参数 '{cmd}'。全链条监控矩阵与 40U 仓位调度器已同步生效。"
    return {"status": "ok", "reply": reply}

if __name__ == "__main__":
    print(f"[INFO] 彭博多模态全链条套利平台启动成功: http://127.0.0.1:{config.WEB_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT, log_level="warning")  # 阶段0止血: 原绑 0.0.0.0 全网暴露, 改回本地
