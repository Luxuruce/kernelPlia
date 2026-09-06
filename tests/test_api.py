"""接口契约里几条与红线直接相关的。"""

from __future__ import annotations

import pathlib
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.api import app  # noqa: E402

client = TestClient(app)


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
