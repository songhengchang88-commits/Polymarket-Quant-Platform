import os

# 基础目录配置 (E盘)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

TRADES_FILE = os.path.join(DATA_DIR, "trades.jsonl")
OPPORTUNITIES_FILE = os.path.join(DATA_DIR, "opportunities.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "system.log")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
NEWS_FILE = os.path.join(DATA_DIR, "news_stream.jsonl")

# Polymarket API
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 运行模式: 'paper' (模拟盘) 或 'live' (实盘)
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "live")  # 默认 live 实盘接入，可在前端/环境变量一键切换
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "0x735e49143B19989E8c2BE88EdE72e96c1d0500dc")
PROXY_WALLET = os.getenv("WALLET_ADDRESS", "0x514F13a326A93AEDA4522B973707e8602c2BCEFd")

# 零亏损铁律与资金调度 (不能亏钱，必须盈利)
INITIAL_BALANCE_USD = float(os.getenv("INITIAL_BALANCE", "36.91"))
ALLOC_PER_TRADE_USD = float(os.getenv("ALLOC_PER_TRADE_USD", "2.6"))   # 单笔 2.6U，确保双腿各自 >= 1.25U，严禁单腿掉单
MIN_ORDER_LEG_USD = 1.15            # 单腿绝对物理红线: Polymarket CLOB 强制单腿 >= $1.00，低于 1.15U 严禁发单
MAX_CONCURRENT_BAGS = 2             # 并发微型稳赢仓位上限
ESTIMATED_GAS_FEE_USD = 0.002       # Polygon 极速 Gas 预估

# --- 资金保护与真实性校验 (2026-11-02 新增) ---
POSITION_CAP_PCT = 0.45             # 单笔最大敞口 = 可用余额 × 45%, 其余恒为安全垫(永不梭哈)
GEOBLOCK_ENFORCE = True             # 受限地区/检测失败一律拒绝发单(fail-closed); 不含任何 IP 规避逻辑
MAKER_FILL_TIMEOUT_SEC = 1800       # 做市挂单最长等待成交时间, 超时撤单并计失败, 不虚构利润
PAPER_MAKER_EDGE_CONFIDENCE = 0.5   # 做市 edge 可信度折扣: 只假设捕获一半价差, 且需双边真实成交

# 100% 必须盈利核心阈值 (纯数学稳赚)
PARITY_ARBITRAGE_THRESHOLD = float(os.getenv("PARITY_ARBITRAGE_THRESHOLD", "0.985"))  # 安全门槛: Ask0 + Ask1 <= 0.985，锁定至少 1.5% 无风险净利
REVERSE_ARB_THRESHOLD = 1.008       # 唯一允许的提前平仓线: Bid0 + Bid1 >= 1.008 (双边均有正收益溢价，绝不向点差割肉)
HOLD_UNTIL_RESOLUTION = True        # 核心防滑点铁律: 严禁市价快进快出跨点差割肉，持有至智能合约 1.000U 法定承兑兜底
MAX_EXPIRY_HOURS = 24               # 短线时效锁: 严格只做 24 小时内必定决胜清算的盘口，资金高效周转
MAKER_SPREAD_MIN_PCT = 2.5          # 双边做市差价门槛

# 扫描与全网极速高频轮动矩阵参数
SCAN_MARKET_LIMIT = 80              # 每次并发监控的市场总数 (80-100个全平台盘口)
POOL_ROTATION_INTERVAL = 10         # 全网大池轮动周期 (10秒高速滚动)
DEFAULT_SCAN_INTERVAL = 0.5         # 轮询采样间隔 (0.5秒极速高频，配合WebSocket零延迟)
NEWS_CHECK_INTERVAL = 10.0          # 突发新闻轮询间隔 (10.0 秒)
DEFAULT_CATEGORY = "all"            # 默认全品类扫描 (涵盖 crypto, sports, politics, pop-culture)

# 快讯 RSS 订阅信源
NEWS_RSS_FEEDS = [
    "https://news.google.com/rss/search?q=crypto+OR+bitcoin+OR+fed+OR+election&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.feedburner.com/CoinDesk",
    "https://cointelegraph.com/rss"
]

WEB_PORT = 8888                     # 彭博多模态终端端口
