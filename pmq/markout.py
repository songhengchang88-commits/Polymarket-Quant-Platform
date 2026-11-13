"""Markout / 逆选择探测。

核心问题: 做市单被成交以后, 价格是继续朝不利方向走, 还是回弹?
如果成交后价格持续下跌, 说明你在接飞刀 —— 纸面模型里那 +1.9%/笔 就不成立。

两种模式:
- dry  (默认): 不报单, 只把"假设我在买一价被动成交"作为基准, 跟踪后续盘口。
              测的是市场结构, 不涉及资金。
- live       : 真实下最小对冲单, 以成交回执为准, 再跟踪 markout。需要私钥。

dry 模式的价值: 它把"假设成交"和"真实成交"之间那条最容易被忽略的缝暴露出来,
而且一分钱风险都不承担。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from pmq.risk import Book


@dataclass
class ProbeSample:
    seconds: float
    bid_sum: float
    mid_sum: float
    markout_pct: float


@dataclass
class ProbeResult:
    market_id: str
    question: str
    entry_bid_sum: float
    entry_mid_sum: float
    samples: List[ProbeSample] = field(default_factory=list)
    verdict: str = ""
    mode: str = "dry"


def judge(samples: Sequence[ProbeSample]) -> str:
    """按最后一个样本的 markout 给出结论。"""
    if not samples:
        return "无样本"
    last = samples[-1].markout_pct
    if last <= -0.5:
        return f"强逆选择: 被动成交后价格继续不利 {last:+.2f}% (做市模型不成立)"
    if last >= 0.5:
        return f"存在回弹: 被动成交后价格修复 {last:+.2f}%"
    return f"中性: markout {last:+.2f}% (接近噪声, 样本需放大)"


class MarkoutProbe:
    def __init__(
        self,
        fetch_pair: Callable[[str, str], tuple],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.fetch_pair = fetch_pair
        self.sleep = sleep
        self.clock = clock

    def run(
        self,
        market_id: str,
        question: str,
        token_0: str,
        token_1: str,
        offsets: Sequence[float],
        mode: str = "dry",
    ) -> ProbeResult:
        book_0, book_1 = self.fetch_pair(token_0, token_1)
        if book_0 is None or book_1 is None:
            raise ValueError("盘口缺失, 无法建立基准")

        entry_bid = book_0.bid + book_1.bid
        entry_mid = book_0.mid + book_1.mid
        started = self.clock()
        result = ProbeResult(
            market_id=market_id,
            question=question,
            entry_bid_sum=round(entry_bid, 6),
            entry_mid_sum=round(entry_mid, 6),
            mode=mode,
        )

        for offset in sorted(float(x) for x in offsets):
            wait = started + offset - self.clock()
            if wait > 0:
                self.sleep(wait)
            later_0, later_1 = self.fetch_pair(token_0, token_1)
            if later_0 is None or later_1 is None:
                continue
            bid_sum = later_0.bid + later_1.bid
            mid_sum = later_0.mid + later_1.mid
            markout = ((bid_sum - entry_bid) / entry_bid * 100.0) if entry_bid > 0 else 0.0
            result.samples.append(ProbeSample(
                seconds=offset,
                bid_sum=round(bid_sum, 6),
                mid_sum=round(mid_sum, 6),
                markout_pct=round(markout, 4),
            ))

        result.verdict = judge(result.samples)
        return result
