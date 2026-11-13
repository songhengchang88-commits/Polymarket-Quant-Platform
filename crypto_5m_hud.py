"""
Polymarket 5 分钟加密量化指挥舰 (方案二升级版：彭博三币实战矩阵 · 全中文 UI)
====================================================================
纯只读消费 data/crypto_5m_live_state.json 与 data/crypto_5m_paper_*.jsonl，
与核心引擎进程物理隔离，确保零干扰、高刷新的全中文实盘监控体验。
特性:
1. 链上实盘真实资金实时透视 (Proxy pUSD 余额 + 签名 EOA POL Gas 监测)
2. 钱包与持仓直达链接 (点击直达 Polymarket Portfolio / 官方市场)
3. 链上 30 秒兑付资金锁定动态看板 (Locked Cash)
4. 跨资产宏观风暴熔断 (Macro Storm) 全局状态告警横幅
5. 悲观滑点惩罚与 Polygon 真实 Gas 摩擦台账穿透
"""

import json
import os
import sys
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
LIVE_STATE_PATH = os.path.join(DATA_DIR, "crypto_5m_live_state.json")
ROUNDS_PATH = os.path.join(DATA_DIR, "crypto_5m_paper_rounds.jsonl")
TRADES_PATH = os.path.join(DATA_DIR, "crypto_5m_paper_trades.jsonl")
SUMMARY_PATH = os.path.join(DATA_DIR, "crypto_5m_paper_summary.json")

app = FastAPI(title="Polymarket 5分钟量化实战指挥舰 v3.1")


def _read_json(path: str, default: dict) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _read_jsonl_tail(path: str, limit: int = 20) -> list:
    items = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    except Exception:
        pass
    return list(reversed(items))


@app.get("/api/dashboard")
async def get_dashboard_data():
    live_state = _read_json(LIVE_STATE_PATH, {})
    summary = _read_json(SUMMARY_PATH, {})
    rounds = _read_jsonl_tail(ROUNDS_PATH, limit=15)
    trades = _read_jsonl_tail(TRADES_PATH, limit=20)
    return JSONResponse({
        "live": live_state,
        "summary": summary,
        "rounds": rounds,
        "trades": trades,
    })


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PolyQuant 5分钟量化实战指挥舰 v3.1</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {
      background-color: #0B0E14;
      color: #E2E8F0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", monospace, sans-serif;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    .panel {
      background-color: #111622;
      border: 1px solid #1E293B;
    }
    .scrollbar-thin::-webkit-scrollbar { width: 5px; height: 5px; }
    .scrollbar-thin::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
  </style>
</head>
<body class="min-h-screen p-3 md:p-4 flex flex-col gap-3">

  <!-- 宏观风暴熔断全局警报横幅 (动态显隐) -->
  <div id="macro-storm-banner" class="hidden rounded-lg bg-rose-500/20 border border-rose-500/60 p-2.5 text-center text-rose-300 text-xs font-bold tracking-wider animate-pulse flex items-center justify-center gap-2">
    <span>🚨 跨资产宏观联动风暴熔断已触发！检测到核心主流币严重单边破位，已秒级撤销被动挂单并强行冻结新开仓！</span>
  </div>

  <!-- 顶部全局控制栏 -->
  <header class="panel rounded-lg p-3 flex flex-wrap items-center justify-between gap-3">
    <div class="flex items-center gap-3">
      <div id="mode-badge" class="px-3 py-1.5 rounded border border-amber-500/50 bg-amber-500/10 text-amber-300 text-xs font-bold tracking-wide">
        <div>[真实数据纸面交易 v3.1]</div>
        <div id="kill-switch-text" class="text-[11px] text-amber-400/90">[物理停机锁已启用 · 零资金风险]</div>
      </div>
      <div>
        <div class="text-xs text-slate-400">拟真总权益 / 可用现金 (含30s兑付锁)</div>
        <div class="flex items-baseline gap-2">
          <span id="equity-val" class="mono text-xl font-bold text-white">$30.00</span>
          <span id="pnl-val" class="mono text-sm font-semibold text-emerald-400">(+0.00U)</span>
          <span id="cash-val" class="mono text-xs text-slate-400">现金: $30.00</span>
          <span id="locked-cash-val" class="mono text-[11px] text-amber-300 bg-amber-950/40 px-1.5 py-0.5 rounded border border-amber-500/30">锁仓: $0.00</span>
        </div>
      </div>
    </div>

    <!-- 链上实盘真实资金透视卡 (支持点击直达 Polymarket 钱包) -->
    <a href="https://polymarket.com/zh/portfolio" target="_blank" rel="noopener noreferrer" 
       title="点击直达官方 Polymarket 钱包资产组合页面"
       class="panel rounded px-3 py-1.5 border border-cyan-500/30 bg-cyan-950/20 hover:bg-cyan-900/30 hover:border-cyan-400/60 transition flex items-center gap-3 text-xs cursor-pointer group">
      <div>
        <div class="text-[10px] text-cyan-400/80 font-semibold group-hover:text-cyan-300 flex items-center gap-1">
          <span>实盘代理合约现金 (Proxy)</span>
          <span class="text-[9px]">↗</span>
        </div>
        <div class="mono font-bold text-cyan-200 text-sm"><span id="live-pusd-val">$1.72</span> <span class="text-[10px] text-cyan-400">pUSD</span></div>
      </div>
      <div class="w-px h-6 bg-cyan-500/20"></div>
      <div>
        <div class="text-[10px] text-cyan-400/80 font-semibold">实盘签名 Gas 燃料 (POL)</div>
        <div class="mono font-bold text-emerald-300 text-sm"><span id="live-pol-val">3.6214</span> <span class="text-[10px] text-slate-400">POL</span></div>
      </div>
      <div id="gas-badge" class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">Gas充沛</div>
    </a>

    <!-- 5分钟轮次倒计时进度条 -->
    <div class="flex-1 min-w-[240px] max-w-sm px-2">
      <div class="flex justify-between text-xs mb-1">
        <span class="text-slate-300 font-semibold">当前周期: <span id="round-clock" class="mono text-emerald-400">--:--</span> 剩余</span>
        <span id="cutoff-badge" class="text-amber-400 font-semibold">尾盘 60 秒全撤保护</span>
      </div>
      <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden relative">
        <div id="round-progress" class="h-full bg-emerald-500 transition-all duration-500" style="width: 100%"></div>
        <div class="absolute top-0 bottom-0 left-[20%] w-0.5 bg-rose-500 z-10" title="最后60秒全撤红线"></div>
      </div>
    </div>

    <!-- 统计与延迟 -->
    <div class="flex items-center gap-4 text-xs">
      <div class="text-right">
        <div class="text-slate-400">已结算轮次 / 胜率</div>
        <div class="mono font-bold text-slate-100"><span id="rounds-stat">0 轮</span> · <span id="winrate-stat" class="text-emerald-400">--%</span></div>
      </div>
      <div class="text-right">
        <div class="text-slate-400">双路采样延迟</div>
        <div class="mono font-bold text-cyan-400"><span id="latency-val">--</span> ms</div>
      </div>
    </div>
  </header>

  <!-- 核心三币并行矩阵 (BTC / ETH / SOL) -->
  <main id="matrix-grid" class="grid grid-cols-1 lg:grid-cols-3 gap-3">
    <!-- 动态渲染 3 个币种卡片 -->
  </main>

  <!-- 底部双栏监控 -->
  <footer class="grid grid-cols-1 lg:grid-cols-12 gap-3 flex-1">
    <!-- 左下：5 秒防毒刷新心跳日志 -->
    <section class="panel rounded-lg p-3 lg:col-span-5 flex flex-col">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-300 flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          5 秒防毒刷新实时心跳日志 (含滑点与兑付锁)
        </h2>
        <span id="last-update" class="mono text-[11px] text-slate-500">同步中...</span>
      </div>
      <div id="log-box" class="bg-[#070A10] border border-slate-800/80 rounded p-2.5 h-56 overflow-y-auto scrollbar-thin mono text-[11px] leading-relaxed text-slate-300 space-y-1">
        <div class="text-slate-500">等待后台纸面引擎推送实时日志...</div>
      </div>
    </section>

    <!-- 右下：已结算 5分钟周期真实盈亏台账 -->
    <section class="panel rounded-lg p-3 lg:col-span-7 flex flex-col">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-300">已结算 5 分钟周期真实盈亏台账 (官方裁决 / 含Gas与滑点)</h2>
        <span id="fills-total" class="mono text-[11px] text-cyan-400">累计成交: 0 笔</span>
      </div>
      <div class="overflow-x-auto h-56 overflow-y-auto scrollbar-thin">
        <table class="w-full text-left border-collapse text-xs">
          <thead class="text-slate-400 border-b border-slate-800 sticky top-0 bg-[#111622]">
            <tr>
              <th class="py-1.5 px-2">周期盘口</th>
              <th class="py-1.5 px-2">基准 → 收盘</th>
              <th class="py-1.5 px-2">胜方</th>
              <th class="py-1.5 px-2">配对锁利</th>
              <th class="py-1.5 px-2">成本/兑付</th>
              <th class="py-1.5 px-2">Gas计提</th>
              <th class="py-1.5 px-2 text-right">净盈亏</th>
            </tr>
          </thead>
          <tbody id="rounds-tbody" class="mono divide-y divide-slate-800/60">
            <tr><td colspan="7" class="py-8 text-center text-slate-500 font-sans">暂无已结束的 5 分钟结算周期，等待首个周期完成自动结算...</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </footer>

<script>
const TAG_ZH = {
  "discount": "折价接盘",
  "panic": "恐慌彩票",
  "high_prob": "高胜率捡漏",
  "hedge": "跨时锁定(带滑点)"
};

function renderSparklineSVG(history, strikeK) {
  if (!history || history.length < 2) {
    return '<div class="h-24 flex items-center justify-center text-xs text-slate-600">采集现货点位中...</div>';
  }
  const w = 360, h = 92, pad = 8;
  const allVals = [...history, strikeK];
  let minV = Math.min(...allVals);
  let maxV = Math.max(...allVals);
  if (maxV - minV < 1e-6) { maxV += 1; minV -= 1; }
  const range = maxV - minV;

  const toY = v => pad + (h - 2 * pad) * (1 - (v - minV) / range);
  const toX = i => pad + (w - 2 * pad) * (i / Math.max(1, history.length - 1));

  const pts = history.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");
  const kY = toY(strikeK).toFixed(1);
  const lastVal = history[history.length - 1];
  const isUp = lastVal >= strikeK;
  const lineColor = isUp ? "#10B981" : "#F43F5E";

  return `
    <svg viewBox="0 0 ${w} ${h}" class="w-full h-24 overflow-visible">
      <line x1="${pad}" y1="${kY}" x2="${w - pad}" y2="${kY}" stroke="#06B6D4" stroke-width="1" stroke-dasharray="4,3" />
      <text x="${w - pad}" y="${Math.max(12, kY - 4)}" fill="#22D3EE" font-size="9" text-anchor="end">基准价 K: ${strikeK}</text>
      <polyline fill="none" stroke="${lineColor}" stroke-width="2" points="${pts}" />
      <circle cx="${toX(history.length - 1).toFixed(1)}" cy="${toY(lastVal).toFixed(1)}" r="3.5" fill="${lineColor}" />
    </svg>
  `;
}

function renderCoinCard(coinKey, c) {
  const diffPct = c.strike_k > 0 ? ((c.spot - c.strike_k) / c.strike_k * 100) : 0;
  const isUp = diffPct >= 0;
  const diffCls = isUp ? "text-emerald-400" : "text-rose-400";
  const diffSign = isUp ? "+" : "";

  const fairUpPct = (c.fair_up * 100).toFixed(0);
  const fairDnPct = (c.fair_dn * 100).toFixed(0);

  const bestBidUp = c.bids_up?.[0]?.[0] ?? "--";
  const bestAskUp = c.asks_up?.[0]?.[0] ?? "--";
  const bestBidDn = c.bids_dn?.[0]?.[0] ?? "--";
  const bestAskDn = c.asks_dn?.[0]?.[0] ?? "--";

  const orders = c.open_orders || [];
  const ordersHtml = orders.length === 0
    ? '<div class="text-slate-500 py-2 text-center text-xs">当前无挂单 (保护中或等待新周期)</div>'
    : orders.map(o => {
        const sideCls = o.side === "UP" ? "text-emerald-300 border-emerald-500/30 bg-emerald-500/10" : "text-rose-300 border-rose-500/30 bg-rose-500/10";
        const tagLabel = TAG_ZH[o.tag] || o.tag;
        return `
          <div class="flex items-center justify-between py-1 px-2 rounded border ${sideCls} text-xs">
            <div class="flex items-center gap-1.5">
              <span class="font-bold">${o.side === "UP" ? "买涨" : "买跌"}</span>
              <span class="text-[10px] px-1 rounded bg-slate-800 text-slate-300">${tagLabel}</span>
            </div>
            <div class="mono font-semibold">
              ${o.shares.toFixed(1)}份 @ $${o.price.toFixed(2)} ($${o.cost_usd.toFixed(2)})
            </div>
          </div>
        `;
      }).join("");

  return `
    <article class="panel rounded-lg p-3 flex flex-col gap-2.5">
      <div class="flex items-center justify-between border-b border-slate-800/80 pb-2">
        <div class="flex items-center gap-2">
          <span class="text-base font-black tracking-wide text-white">${c.coin}</span>
          <span class="text-xs text-slate-400">5分钟短线</span>
        </div>
        <div class="text-right flex items-center gap-2">
          <div>
            <span class="mono text-base font-bold text-white">$${c.spot.toFixed(2)}</span>
            <span class="mono text-xs font-semibold ${diffCls} ml-1.5">${diffSign}${diffPct.toFixed(2)}%</span>
          </div>
          <a href="https://polymarket.com/market/${c.slug}" target="_blank" rel="noopener noreferrer" 
             title="点击在 Polymarket 官网查看该市场" 
             class="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700">
            官网盘口 ↗
          </a>
        </div>
      </div>

      <div class="bg-[#070A10] border border-slate-800/60 rounded p-1">
        ${renderSparklineSVG(c.spot_history, c.strike_k)}
      </div>

      <div class="grid grid-cols-2 gap-2 text-xs">
        <div class="bg-slate-900/60 border border-slate-800 rounded p-2">
          <div class="text-[10px] text-slate-400 mb-1 flex justify-between">
            <span>官方买一/卖一 (UP/DN)</span>
            <span class="text-cyan-400">1.8x排队惩罚</span>
          </div>
          <div class="mono text-[11px] flex justify-between text-slate-200">
            <span>涨: ${bestBidUp} - ${bestAskUp}</span>
            <span>跌: ${bestBidDn} - ${bestAskDn}</span>
          </div>
        </div>

        <div class="bg-slate-900/60 border border-slate-800 rounded p-2">
          <div class="text-[10px] text-slate-400 mb-1">公允概率定价 (BS模型+OFI)</div>
          <div class="flex items-center gap-2">
            <div class="flex-1 h-3 bg-slate-800 rounded-sm overflow-hidden flex">
              <div class="bg-emerald-500 h-full" style="width: ${fairUpPct}%"></div>
              <div class="bg-rose-500 h-full" style="width: ${fairDnPct}%"></div>
            </div>
            <span class="mono text-[11px] font-bold text-slate-200">${fairUpPct}% / ${fairDnPct}%</span>
          </div>
        </div>
      </div>

      <!-- 当前持仓栏 (支持点击直达 Polymarket Portfolio 官方持仓页面) -->
      <a href="https://polymarket.com/zh/portfolio" target="_blank" rel="noopener noreferrer"
         title="点击直达官方 Polymarket 持仓与资产组合页面"
         class="bg-slate-900/40 hover:bg-slate-800/60 border border-slate-800/60 hover:border-cyan-500/40 rounded p-2 text-xs flex justify-between items-center transition cursor-pointer group">
        <div>
          <span class="text-slate-400 group-hover:text-cyan-300">当前持仓 ↗:</span>
          <span class="mono text-emerald-400 font-bold ml-1">${c.up_shares.toFixed(1)} 涨</span>
          <span class="text-slate-500">/</span>
          <span class="mono text-rose-400 font-bold">${c.down_shares.toFixed(1)} 跌</span>
        </div>
        <div>
          <span class="text-slate-400">已配对锁利:</span>
          <span class="mono text-cyan-300 font-bold ml-1">${c.paired_shares.toFixed(1)} 份</span>
        </div>
      </a>

      <div>
        <div class="text-[11px] font-semibold text-slate-400 mb-1.5 flex justify-between items-center">
          <span>安全网格智能挂单 (自适应对冲)</span>
          <span class="text-[10px] text-slate-500">${orders.length} 笔等待撮合</span>
        </div>
        <div class="space-y-1.5">
          ${ordersHtml}
        </div>
      </div>
    </article>
  `;
}

async function fetchAndRender() {
  try {
    const res = await fetch("/api/dashboard");
    if (!res.ok) return;
    const data = await res.json();
    const live = data.live || {};
    const sum = data.summary || {};

    // 宏观风暴指示灯
    const banner = document.getElementById("macro-storm-banner");
    if (live.macro_storm_active) {
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    // 链上资金透视
    const cf = live.live_chain_funds || {};
    if (cf.proxy_pusd !== undefined) {
      document.getElementById("live-pusd-val").textContent = `$${Number(cf.proxy_pusd).toFixed(2)}`;
    }
    if (cf.wallet_pol !== undefined) {
      document.getElementById("live-pol-val").textContent = Number(cf.wallet_pol).toFixed(4);
    }
    const gasBadge = document.getElementById("gas-badge");
    if (cf.gas_ok === false) {
      gasBadge.className = "px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40 animate-pulse";
      gasBadge.textContent = "Gas不足需充";
    } else {
      gasBadge.className = "px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40";
      gasBadge.textContent = "Gas充沛";
    }

    // 拟真资金
    const eq = sum.current_equity_usd ?? live.current_equity_usd ?? 30.0;
    const initCap = sum.initial_capital_usd ?? live.initial_capital_usd ?? 30.0;
    const netPnl = sum.total_settled_pnl_usd ?? live.total_settled_pnl_usd ?? 0.0;
    const roi = sum.roi_pct ?? (netPnl / initCap * 100);

    document.getElementById("equity-val").textContent = `$${Number(eq).toFixed(2)}`;
    const pnlEl = document.getElementById("pnl-val");
    pnlEl.textContent = `(${netPnl >= 0 ? "+" : ""}${Number(netPnl).toFixed(2)}U / ${roi.toFixed(1)}%)`;
    pnlEl.className = `mono text-sm font-semibold ${netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`;
    document.getElementById("cash-val").textContent = `可用: $${Number(live.paper_cash_usd ?? 30.0).toFixed(2)}`;
    document.getElementById("locked-cash-val").textContent = `30s锁仓: $${Number(live.locked_cash_usd ?? 0.0).toFixed(2)}`;

    // 统计
    const totRounds = sum.total_rounds ?? live.total_rounds ?? 0;
    const winRate = sum.win_rate_pct ?? (totRounds > 0 ? (live.winning_rounds / totRounds * 100) : 0);
    document.getElementById("rounds-stat").textContent = `${totRounds} 轮`;
    document.getElementById("winrate-stat").textContent = `${Number(winRate).toFixed(1)}%`;
    document.getElementById("latency-val").textContent = live.latency_ms ?? "--";
    document.getElementById("fills-total").textContent = `累计成交: ${live.total_fills ?? 0} 笔`;
    document.getElementById("last-update").textContent = `更新于 ${live.updated_at || "--"}`;

    // 倒计时
    const remSec = live.rem_sec ?? 300;
    const mins = Math.floor(remSec / 60);
    const secs = Math.floor(remSec % 60);
    document.getElementById("round-clock").textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
    const pct = Math.max(0, Math.min(100, (remSec / 300) * 100));
    const progEl = document.getElementById("round-progress");
    progEl.style.width = `${pct}%`;
    const cutBadge = document.getElementById("cutoff-badge");
    if (remSec <= 60) {
      progEl.className = "h-full bg-rose-500 transition-all duration-500";
      cutBadge.textContent = "🚨 尾盘60秒全撤保护生效中";
      cutBadge.className = "text-rose-400 font-bold animate-pulse";
    } else {
      progEl.className = "h-full bg-emerald-500 transition-all duration-500";
      cutBadge.textContent = "尾盘 60 秒全撤红线";
      cutBadge.className = "text-amber-400 font-semibold";
    }

    // 三币矩阵
    const coinsObj = live.coins || {};
    const grid = document.getElementById("matrix-grid");
    const keys = ["btc", "eth", "sol"];
    if (Object.keys(coinsObj).length > 0) {
      grid.innerHTML = keys.filter(k => coinsObj[k]).map(k => renderCoinCard(k, coinsObj[k])).join("");
    }

    // 心跳日志
    const logs = live.recent_logs || [];
    const logBox = document.getElementById("log-box");
    if (logs.length > 0) {
      logBox.innerHTML = logs.slice().reverse().map(l => `<div>${l}</div>`).join("");
    }

    // 结算台账
    const roundsList = data.rounds || [];
    const tbody = document.getElementById("rounds-tbody");
    if (roundsList.length > 0) {
      tbody.innerHTML = roundsList.map(r => {
        const pnlCls = r.realized_pnl_usd >= 0 ? "text-emerald-400" : "text-rose-400";
        const winZh = r.winner === "UP" ? "看涨 UP" : "看跌 DOWN";
        return `
          <tr class="hover:bg-slate-800/40">
            <td class="py-1.5 px-2 text-slate-200">${r.slug}</td>
            <td class="py-1.5 px-2 text-slate-400">${Number(r.open_price).toFixed(2)} → ${Number(r.close_price).toFixed(2)}</td>
            <td class="py-1.5 px-2 font-bold ${r.winner === "UP" ? "text-emerald-400" : "text-rose-400"}">${winZh}</td>
            <td class="py-1.5 px-2 text-cyan-300 font-bold">${Number(r.paired_shares).toFixed(1)} 份</td>
            <td class="py-1.5 px-2">$${Number(r.total_cost_usd).toFixed(2)} / $${Number(r.payout_usd).toFixed(2)}</td>
            <td class="py-1.5 px-2 text-slate-400">-$0.035U</td>
            <td class="py-1.5 px-2 text-right font-bold ${pnlCls}">${r.realized_pnl_usd >= 0 ? "+" : ""}${Number(r.realized_pnl_usd).toFixed(4)}U</td>
          </tr>
        `;
      }).join("");
    }
  } catch (e) {
    console.error(e);
  }
}

fetchAndRender();
setInterval(fetchAndRender, 2000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_PAGE)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    uvicorn.run(app, host="127.0.0.1", port=18899, log_level="warning")
