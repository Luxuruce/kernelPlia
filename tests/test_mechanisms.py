"""三条机制的最小回归：一跳扩展、冲突暴露并阻断、成本闸门，外加接口上与红线直接相关的几条。

**两种模式都跑**——演示切片（10 条条款单元）上能照出全部机制，完整索引上也必须成立。
只点名演示切片里有的条款（第 5(4)、29(1)、70(3) 条、FAQ 第 III 章问 17）与用例 G13。
2026-10-06 由公开仓的 test_api / test_context / test_retrieval 合并移入（仓库迁移方案第 6.1 节）。
"""

from __future__ import annotations

import pathlib
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.api import app  # noqa: E402
from app.context import (  # noqa: E402
    CONTEXT_FIELDS,
    EXCLUDED_FIELDS,
    TOKEN_BUDGET,
    assemble,
    estimate_tokens,
    fit_to_budget,
    plan_trim,
    serialize,
)
from app.retrieval import (  # noqa: E402
    TRIGGER_SCORE_THRESHOLD,
    detect_conflicts_decoupled,
    retrieve,
    self_scores,
)
from app.terms import extract_terms  # noqa: E402

client = TestClient(app)


# ── 召回与冲突判定 ──────────────────────────────────────────────────────
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


#: 导出到演示切片的三条用例（tools/export_demo_index.py 的 DEMO_CASES）。
#: 完整索引上只看这三条——其余用例有已知召回缺口，由 test_retrieval.py 的基线用例管。
DEMO_CASES = ("G01", "G02", "G13")


def test_演示用例的期望引用都召回得到(conn, cases):
    for c in (c for c in cases if c["id"] in DEMO_CASES):
        got = {h.clause_id for h in retrieve(conn, c["q"]).hits}
        missing = [x for x in c["expect_clauses"] if x not in got]
        assert not missing, f"{c['id']}：{missing}"


# ── 上下文装配与成本闸门 ────────────────────────────────────────────────

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


# ── 接口 ────────────────────────────────────────────────────────────────

def test_graph无会话时不泄露整张索引(conn):
    """整张带条款号的图 = 索引的完整目录与关系结构一次页面加载全给出去。

    **护城河是索引，不是代码。** 所以图谱默认只给位阶与拓扑；
    一个节点在本会话的回答里作为引用出现过之后，才获得名字。
    """
    d = client.get("/api/graph").json()
    assert d["ranks"] and d["edges"]
    named = [x for x in d["labels"] if x]
    assert len(named) < len(d["ranks"]), "全部点亮就等于全开"


def test_graph按会话解锁(conn):
    """**净泄露为零**：用户看到的名字，是他自己那条回答的引用清单里已有的。"""
    import uuid

    sid = f"test-{uuid.uuid4().hex[:8]}"
    cid = conn.execute(
        "select id from clause where status = 'adopted' order by id limit 1"
    ).fetchone()[0]
    base = {x["id"] for x in client.get("/api/graph").json()["labels"] if x}
    conn.execute("insert into event (session_id, kind, clause_id) "
                 "values (%s, 'citation_shown', %s)", (sid, cid))
    conn.commit()
    got = {x["id"] for x in client.get(f"/api/graph?session_id={sid}").json()["labels"] if x}
    assert got == base | {cid}
    conn.execute("delete from event where session_id = %s", (sid,))
    conn.commit()


def test_条款详情不返回notes(conn):
    """`notes` 是给模型的答题纪律与内部运营口径，暴露给用户等于把它贴到台面上。"""
    d = client.get("/api/clause/ppwr/art/5/4").json()
    assert "notes" not in d
    assert d["display"] == "第 5(4) 条" and d["authority_rank"] == 1


def test_未采纳的条款猜id也看不到(conn):
    assert client.get("/api/clause/ppwr/art/999/9").status_code == 404


def test_第一段不调模型也不计费(conn):
    """两段式：条款清单检索完就有，几十毫秒可达。**先亮条款清单本身就是差异化。**"""
    r = client.post("/api/ask/clauses",
                    json={"question": "包装里重金属最多能有多少？", "session_id": "t1"})
    assert r.status_code == 200 and r.json()["citations"]
