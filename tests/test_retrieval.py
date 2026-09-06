"""召回与冲突判定。跑在演示切片上——10 条条款单元，足够把三条机制都照出来。"""

from __future__ import annotations

from app.retrieval import (
    TRIGGER_SCORE_THRESHOLD,
    detect_conflicts_decoupled,
    retrieve,
    self_scores,
)
from app.terms import extract_terms


def test_必扩三类是无条件双向的(conn):
    """`derogates` / `amends` / `depends_on_pending` 缺了是**错答不是漏答**：
    只答限值不带减损，会让本来豁免的人得到错误结论。"""
    r = retrieve(conn, "包装里重金属最多能有多少？")
    via = {h.clause_id: (h.via or "") for h in r.hits}
    assert "ppwr/art/70/3" in via and "derogates" in via["ppwr/art/70/3"]


def test_explains上扩把法条带回来(conn):
    """命中一条位阶 4 的 FAQ，要把它解释的那条法条带回来——
    否则答案只剩「委员会解释、无法律约束力」，拿不出法条依据。"""
    r = retrieve(conn, "包装里重金属最多能有多少？")
    assert "ppwr_faq/ch/III/q/17" in {h.clause_id for h in r.hits}


def test_冲突暴露并阻断_不自动择一(conn, cases):
    """官方文件互相打架时**拦住用户，而不是替他选一个**。"""
    g13 = next(c for c in cases if c["id"] == "G13")
    r = retrieve(conn, g13["q"])
    assert r.conflict_ids == g13["expect_conflict_ids"]


def test_触发与召回解耦(conn):
    """**阻断只看触发条款自身的自足分数，完全不看命中集。**

    这是刻意的：接在 top-N 上的话，索引一变大、竞争一激烈，
    同一句提问就会时而阻断时而不阻断——而阻断是红线级的确定性要求。
    """
    q = "托盘缠绕膜要满足重复使用目标吗？"
    sc = self_scores(conn, extract_terms(q), ["ppwr/art/29/1"])
    assert sc["ppwr/art/29/1"] >= TRIGGER_SCORE_THRESHOLD
    assert detect_conflicts_decoupled(conn, q, extract_terms(q))


def test_无关提问不得误阻断(conn):
    assert detect_conflicts_decoupled(conn, "今天天气怎么样？", extract_terms("今天天气怎么样？")) == []


def test_演示用例的期望引用都召回得到(conn, cases):
    for c in cases:
        got = {h.clause_id for h in retrieve(conn, c["q"]).hits}
        missing = [x for x in c["expect_clauses"] if x not in got]
        assert not missing, f"{c['id']}：{missing}"
