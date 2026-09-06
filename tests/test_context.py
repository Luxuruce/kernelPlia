"""上下文装配与成本闸门。"""

from __future__ import annotations

from app.context import (
    CONTEXT_FIELDS,
    EXCLUDED_FIELDS,
    TOKEN_BUDGET,
    assemble,
    estimate_tokens,
    fit_to_budget,
    plan_trim,
    serialize,
)
from app.retrieval import retrieve


def test_notes与页图坐标不进模型上下文(conn):
    """`notes` 是内部答题纪律，`bbox` 一个单元就 1 KB——
    少一个字段是功能缺失，多一个字段是白烧 token 且可能泄露内部口径。"""
    for f in EXCLUDED_FIELDS:
        assert f not in CONTEXT_FIELDS


def test_装配不越成本闸门(conn):
    for q in ("包装里重金属最多能有多少？", "托盘缠绕膜要满足重复使用目标吗？"):
        ctx, _ = fit_to_budget(assemble(conn, retrieve(conn, q)))
        assert estimate_tokens(serialize(ctx)) <= TOKEN_BUDGET


def test_派生不得优先于来源(conn):
    """上扩进来的条款不得排在把它带进来的那一条之前。

    只按类别排档位守不住——档位不区分「派生与来源同时在场」和
    「来源已经被丢掉」，切口落在中间就会留下一条没有法条依据的解释。
    """
    from app.context import _upward_source

    items = assemble(conn, retrieve(conn, "包装里重金属最多能有多少？"))
    order, _ = plan_trim(items)
    pos = {cid: n for n, (_, ids) in enumerate(order) for cid in ids}
    for i in items:
        src = _upward_source(i)
        if src and src in pos:
            assert pos[src] <= pos[i.data["id"]], f"{i.data['id']} 排在了来源 {src} 之前"


def test_位阶锚点与它的解释同进同出(conn):
    """一条 rank 3/4 的解释与它上扩出来的 rank 1/2 法条，
    是裁剪意义上**不可分割的一个单元**：要么都在，要么都不在。"""
    from app.context import _upward_source

    items = assemble(conn, retrieve(conn, "包装里重金属最多能有多少？"))
    kept, _ = fit_to_budget(items)
    kept_ids = {i.data["id"] for i in kept}
    by_id = {i.data["id"]: i for i in items}
    for i in items:
        src = _upward_source(i)
        if src in by_id and by_id[src].data["authority_rank"] >= 3 \
                and i.data["authority_rank"] <= 2:
            assert (src in kept_ids) == (i.data["id"] in kept_ids)


def test_单条用例不得丢光全部期望引用(conn, cases):
    """**丢的不是 20%，是 100%，性质就不一样了。**

    期望引用被丢光时系统只剩两条路：拒答，或者拿不相干的条款去回答——
    后者是错答，比拒答坏得多，而且看起来引用充分、位阶齐全。
    """
    for c in cases:
        ctx, _ = fit_to_budget(assemble(conn, retrieve(conn, c["q"])))
        ids = {i.data["id"] for i in ctx}
        assert set(c["expect_clauses"]) & ids, f"{c['id']} 一条期望引用都没剩下"
