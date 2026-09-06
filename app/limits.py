"""限流与预算闸门。规格出自 产品契约文档 第六节表内（2026-09-06 拍板 该项裁决）。

两件事，都**存在 PostgreSQL 里，不引入 Redis**——展示版量级下
「能少一个组件就少一个」成立，`event` 表本来就有 `session_id` 与时间戳，加表即可。

    每 IP 每日 5 次      滥用防护（防爬、防刷），**不是额度保命措施**
    月度预算两级         70% 告警、100% 降级；**不硬停**

`over_budget` 与 `rate_limited` 都不返回错误页——降级态「比没上线更伤」，
但降级不等于白屏：两段式的第一段（条款清单 + 原文页高亮）本来就是差异化所在，
所以超限时只出第一段、不调模型。
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

#: 每 IP 每日问答次数。5 次的依据（产品契约文档 第六节）：真实用户的好奇心驱动 3–10 次够用；
#: 5 次能把一次百人级传播的消耗压在 500 问内。
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT_PER_IP", "5"))

#: 月度预算（元）。**产品把口径定成金额、没有给数**——
#: 这个默认值是从他们推导每日 5 次时用的「每天约 ¥20」反推的（¥20 × 30 = ¥600）。
#: 产品契约文档 待办表里「充值额度定下来后回来复核」那条同时管这两个数。
MONTHLY_BUDGET_CNY = float(os.getenv("MONTHLY_BUDGET_CNY", "600"))

WARN_AT = 0.70          # 告警，通知人，给充值留时间
DEGRADE_AT = 1.00       # 超限降级：只出第一段，不调模型

#: 火山方舟定价（元 / 百万 token），2026-09-06 控制台口径。
#: 实测单问成本：lite ¥0.011、pro 默认配置 ¥0.478（后者绝大部分是思考流）。
PRICE_CNY_PER_MTOK = {
    "doubao-seed-2-0-lite-260428": {"in": 0.30, "out": 3.00},
    "doubao-seed-2-1-pro-260628": {"in": 2.00, "out": 20.00},
}
#: 未登记的模型按最贵的算——**估错方向要偏保守**，宁可早降级不可超支。
_FALLBACK_PRICE = {"in": 2.00, "out": 20.00}


def cost_cny(model: str, in_tok: int, out_tok: int) -> float:
    p = PRICE_CNY_PER_MTOK.get(model, _FALLBACK_PRICE)
    return (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000


@dataclass
class Gate:
    """这一轮该不该调模型，以及为什么。"""

    allowed: bool
    reason: str | None          # rate_limited / budget_exhausted / None
    used_today: int
    month_cny: float
    budget_cny: float

    @property
    def budget_ratio(self) -> float:
        return self.month_cny / self.budget_cny if self.budget_cny else 0.0

    @property
    def warn(self) -> bool:
        return self.budget_ratio >= WARN_AT


def month_key(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")


def check(conn, ip: str) -> Gate:
    """查闸门。**只读**——计数在 `consume` 里加，避免被拦下的请求也计费。"""
    today = dt.date.today()
    used = conn.execute(
        "select n from rate_limit where ip = %s and day = %s", (ip, today)
    ).fetchone()
    used = used[0] if used else 0

    spent = conn.execute(
        "select coalesce(sum(cny), 0) from spend where month = %s", (month_key(),)
    ).fetchone()[0]
    spent = float(spent)

    if spent >= MONTHLY_BUDGET_CNY * DEGRADE_AT:
        reason = "budget_exhausted"
    elif used >= DAILY_LIMIT:
        reason = "rate_limited"
    else:
        reason = None
    return Gate(reason is None, reason, used, spent, MONTHLY_BUDGET_CNY)


def consume(conn, ip: str) -> None:
    """记一次问答。**只有真的调了模型才叫一次**——被闸门拦下的不计。"""
    conn.execute(
        """insert into rate_limit (ip, day, n) values (%s, %s, 1)
           on conflict (ip, day) do update set n = rate_limit.n + 1""",
        (ip, dt.date.today()),
    )


def record_spend(conn, model: str, in_tok: int, out_tok: int) -> float:
    """把这一次的花费累进月度账。返回本次金额（元）。"""
    c = cost_cny(model, in_tok, out_tok)
    conn.execute(
        """insert into spend (month, model, calls, in_tok, out_tok, cny)
           values (%s, %s, 1, %s, %s, %s)
           on conflict (month, model) do update set
             calls = spend.calls + 1,
             in_tok = spend.in_tok + excluded.in_tok,
             out_tok = spend.out_tok + excluded.out_tok,
             cny = spend.cny + excluded.cny""",
        (month_key(), model, in_tok, out_tok, c),
    )
    return c
