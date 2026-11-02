import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open(r"E:\Polymarket-Quant-Platform\data\latest_markets.json", "r", encoding="utf-8") as f:
    markets = json.load(f)

print(f"Total markets in cache: {len(markets)}")
costs = [m.get("pair_cost", 1.0) for m in markets if m.get("pair_cost", 1.0) > 0]
if costs:
    print(f"Min pair_cost: {min(costs):.4f}")
    print(f"Max pair_cost: {max(costs):.4f}")
    print(f"Average pair_cost: {sum(costs)/len(costs):.4f}")

sorted_m = sorted(markets, key=lambda x: x.get("pair_cost", 1.0))
print("\n=== Top 8 Lowest Cost Markets (Taker Pair Cost) ===")
for m in sorted_m[:8]:
    cost = m.get("pair_cost")
    margin = (1.0 - cost) * 100
    q = m.get("question", "")[:60]
    print(f"Cost: {cost:.4f} (Margin: {margin:+.2f}%) | {q}")

sorted_maker = sorted(markets, key=lambda x: x.get("maker_edge_pct", 0.0), reverse=True)
print("\n=== Top 8 Highest Spread Markets (Maker Opportunities) ===")
for m in sorted_maker[:8]:
    edge = m.get("maker_edge_pct", 0.0)
    b0, a0 = m.get("best_bid_0", 0), m.get("best_ask_0", 0)
    b1, a1 = m.get("best_bid_1", 0), m.get("best_ask_1", 0)
    q = m.get("question", "")[:50]
    print(f"Maker Edge: +{edge:.1f}% | YES: {b0:.3f}/{a0:.3f} | NO: {b1:.3f}/{a1:.3f} | {q}")
