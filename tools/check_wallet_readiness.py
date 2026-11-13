import os
import sys
import time
import json
import requests
from dotenv import load_dotenv

# 确保控制台中文输出正常
sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
load_dotenv(ENV_PATH)

def mask_addr(addr: str) -> str:
    if not addr or len(addr) < 10:
        return "[未配置]"
    return f"{addr[:6]}...{addr[-4:]}"

def mask_key(k: str) -> str:
    if not k or len(k) < 10:
        return "[未配置]"
    return f"{k[:4]}...{k[-4:]} (已脱敏保护)"

print("=" * 60)
print("🛡️  Polymarket 准实盘前置环境与钱包链路零风险自检")
print(f"检测时间: {time.strftime('%Y-%m-%d %H:%M:%S')} (Local Time / UTC+8)")
print("安全状态: 严格只读模式 (0 资金消耗，绝不发起真实交易)")
print("=" * 60)

issues_to_fix = []
passed_items = []

# --- 1. 官方 API 与网络基础设施延迟自检 ---
print("\n📡 [1/4] 官方 API 与网络节点延迟自检")

# 1.1 Polymarket CLOB API
t0 = time.time()
try:
    r = requests.get("https://clob.polymarket.com/time", timeout=5)
    rtt = int((time.time() - t0) * 1000)
    if r.status_code == 200:
        res_data = r.json()
        server_time = res_data.get("time", "") if isinstance(res_data, dict) else str(res_data)
        print(f"  ✅ 官方 CLOB API (clob.polymarket.com): 连通正常 (RTT: {rtt}ms, 服务端时间: {server_time})")
        passed_items.append(f"官方 CLOB API 通畅 (延迟 {rtt}ms)")
    else:
        print(f"  ❌ 官方 CLOB API 异常: HTTP {r.status_code}")
        issues_to_fix.append("官方 CLOB API 响应异常")
except Exception as e:
    print(f"  ❌ 官方 CLOB API 连接失败: {e}")
    issues_to_fix.append("无法连接官方 CLOB API，请检查公网出口代理")

# 1.2 Polymarket Gamma API
t0 = time.time()
try:
    r = requests.get("https://gamma-api.polymarket.com/events?limit=1", timeout=5)
    rtt = int((time.time() - t0) * 1000)
    if r.status_code == 200:
        print(f"  ✅ 官方 Gamma API (gamma-api.polymarket.com): 连通正常 (RTT: {rtt}ms)")
        passed_items.append(f"官方 Gamma API 通畅 (延迟 {rtt}ms)")
    else:
        print(f"  ❌ 官方 Gamma API 异常: HTTP {r.status_code}")
        issues_to_fix.append("官方 Gamma API 响应异常")
except Exception as e:
    print(f"  ❌ 官方 Gamma API 连接失败: {e}")
    issues_to_fix.append("无法连接官方 Gamma API")

# 1.3 Polygon RPC 节点
rpc_urls = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]
best_rpc = None
best_rtt = 9999
current_block = 0

for rpc in rpc_urls:
    t0 = time.time()
    try:
        payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        r = requests.post(rpc, json=payload, timeout=4)
        rtt = int((time.time() - t0) * 1000)
        if r.status_code == 200 and "result" in r.json():
            blk = int(r.json()["result"], 16)
            if rtt < best_rtt:
                best_rtt = rtt
                best_rpc = rpc
                current_block = blk
            print(f"  ✅ Polygon RPC [{rpc}]: 通畅 (RTT: {rtt}ms, 块高: {blk})")
    except Exception:
        print(f"  ⚠️ Polygon RPC [{rpc}]: 节点超时或失败")

if best_rpc:
    passed_items.append(f"Polygon RPC 节点就绪 (最优: {best_rpc}, RTT: {best_rtt}ms)")
else:
    issues_to_fix.append("所有公共 Polygon RPC 均超时，建议配置独立专有 RPC (如 Alchemy/Infura)")

# --- 2. 本地钱包凭证与签名类型自检 ---
print("\n🔑 [2/4] 本地钱包配置与签名体系自检")
funder_addr = os.getenv("FUNDER_ADDRESS", "").strip()
wallet_addr = os.getenv("WALLET_ADDRESS", "").strip()
priv_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
sig_type = os.getenv("POLY_SIGNATURE_TYPE", "0").strip()

print(f"  • FUNDER_ADDRESS: {mask_addr(funder_addr)}")
print(f"  • WALLET_ADDRESS: {mask_addr(wallet_addr)}")
print(f"  • POLY_PRIVATE_KEY: {mask_key(priv_key)}")
print(f"  • POLY_SIGNATURE_TYPE: {sig_type} (0=EOA独立私钥, 1=PolyProxy, 2=GnosisSafe)")

if not priv_key or "YourPrivateKey" in priv_key:
    issues_to_fix.append("未在 .env 中配置 POLY_PRIVATE_KEY")
    print("  ⚠️ 提示: .env 中尚未填入有效私钥（当前为纯纸面环境，符合安全要求）")
else:
    try:
        from eth_account import Account
        acct = Account.from_key(priv_key)
        derived_addr = acct.address
        print(f"  ✅ 本地私钥格式正确，派生钱包地址: {mask_addr(derived_addr)}")
        if wallet_addr and wallet_addr.lower() != derived_addr.lower():
            print(f"  ⚠️ 警告: WALLET_ADDRESS 与私钥派生地址不一致 (派生: {mask_addr(derived_addr)})")
            issues_to_fix.append("WALLET_ADDRESS 与 POLY_PRIVATE_KEY 派生地址不匹配")
        else:
            passed_items.append("本地私钥与钱包地址匹配有效")
    except Exception as e:
        print(f"  ❌ 私钥格式校验失败: {e}")
        issues_to_fix.append(f"私钥格式错误: {e}")

# --- 3. 官方 CLOB API 密钥派生通道自检 (EIP-712 签名能力) ---
print("\n🤝 [3/4] 官方 CLOB 凭证派生与 EIP-712 签名能力自检")
if priv_key and "YourPrivateKey" not in priv_key:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        print("  ⏳ 正在尝试向官方服务器验证 EIP-712 凭证派生 (0资金消耗)...")
        t0 = time.time()
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=priv_key,
            chain_id=POLYGON,
            signature_type=int(sig_type) if sig_type.isdigit() else 0,
            funder=funder_addr if funder_addr else None,
        )
        api_creds = client.derive_api_key()
        rtt = int((time.time() - t0) * 1000)
        print(f"  ✅ 官方 EIP-712 握手成功！成功派生 CLOB API Key: {api_creds.api_key[:8]}... (耗时 {rtt}ms)")
        passed_items.append("官方 CLOB API 凭证派生与 EIP-712 签名握手 100% 成功")
    except Exception as e:
        print(f"  ❌ 官方 CLOB 凭证派生失败: {e}")
        issues_to_fix.append(f"官方 CLOB API 凭证派生握手失败: {e}")
else:
    print("  ⏭️ 私钥未配置，跳过 CLOB API 握手检测（暂缓待填）")
    issues_to_fix.append("待配置有效私钥后完成官方 CLOB 握手验证")

# --- 4. Polygon 链上资产与智能合约授权状态自检 ---
print("\n💰 [4/4] Polygon 链上资产与智能合约授权核验")
check_addr = wallet_addr if wallet_addr and "YourWallet" not in wallet_addr else funder_addr

if best_rpc and check_addr and "0x" in check_addr:
    print(f"  • 查询目标链上地址: {check_addr}")
    
    # 4.1 Native POL (Gas) 余额
    try:
        payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [check_addr, "latest"], "id": 1}
        r = requests.post(best_rpc, json=payload, timeout=4).json()
        pol_wei = int(r.get("result", "0x0"), 16)
        pol_balance = pol_wei / 10**18
        print(f"  • POL (Matic Gas 费): {pol_balance:.4f} POL")
        if pol_balance < 0.05:
            print("  ⚠️ 警告: 链上 POL 余额过低 (< 0.05 POL)，可能不足以支付合约授权或赎回 Gas 费！")
            issues_to_fix.append("POL (Gas费) 余额不足 0.05，需小额转入备用 Gas")
        else:
            passed_items.append(f"POL Gas 充足 ({pol_balance:.4f} POL)")
    except Exception as e:
        print(f"  ⚠️ POL 余额查询异常: {e}")

    # 4.2 USDC.e (Bridged USDC) 余额与授权
    # USDC.e 合约地址 (Polygon): 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    # Polymarket CTF Exchange 合约: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
    usdc_e_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    ctf_exchange = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    
    # ERC-20 balanceOf(address) -> selector 0x70a08231
    clean_addr = check_addr.lower().replace("0x", "").zfill(64)
    data_balance = f"0x70a08231{clean_addr}"
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_e_contract, "data": data_balance}, "latest"],
            "id": 1,
        }
        r = requests.post(best_rpc, json=payload, timeout=4).json()
        raw_bal = int(r.get("result", "0x0"), 16)
        usdc_balance = raw_bal / 10**6
        print(f"  • USDC.e 余额: ${usdc_balance:.2f} USDC")
        if usdc_balance <= 0.0:
            issues_to_fix.append("钱包内 USDC.e 余额为 0（Polygon 链）")
        else:
            passed_items.append(f"USDC.e 余额正常 (${usdc_balance:.2f})")
    except Exception as e:
        print(f"  ⚠️ USDC.e 余额查询异常: {e}")

    # ERC-20 allowance(owner, spender) -> selector 0xdd62ed3e
    clean_spender = ctf_exchange.lower().replace("0x", "").zfill(64)
    data_allowance = f"0xdd62ed3e{clean_addr}{clean_spender}"
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_e_contract, "data": data_allowance}, "latest"],
            "id": 1,
        }
        r = requests.post(best_rpc, json=payload, timeout=4).json()
        raw_allowance = int(r.get("result", "0x0"), 16)
        allowance_usd = raw_allowance / 10**6
        print(f"  • Polymarket CTF Exchange 授权额度: ${allowance_usd:.2f} USDC")
        if allowance_usd < 10.0:
            print("  ⚠️ 警告: USDC.e 尚未对 Polymarket 合约充分授权 (Allowance < $10.0)")
            issues_to_fix.append("USDC.e 尚未对 Polymarket CTF Exchange 进行链上 Approve 授权")
        else:
            passed_items.append(f"USDC.e 智能合约授权充足 (${allowance_usd:.0f})")
    except Exception as e:
        print(f"  ⚠️ 授权额度查询异常: {e}")
else:
    print("  ⏭️ 未配置有效钱包地址，跳过链上资产与授权检测")
    issues_to_fix.append("待填入钱包地址后检测链上 POL/USDC.e 与合约授权")

# --- 5. 总结诊断与消除清单 ---
print("\n" + "=" * 60)
print("📋 【实战前置准备就绪诊断汇总】")
print("=" * 60)
print(f"🟢 已合格项 ({len(passed_items)} 项):")
for item in passed_items:
    print(f"   ✓ {item}")

print(f"\n🔴 待消除隐患与配置缺项 ({len(issues_to_fix)} 项):")
for idx, issue in enumerate(issues_to_fix, 1):
    print(f"   [{idx}] {issue}")

print("=" * 60)
