"""提问怎么切成主题词。**不依赖库**。"""

from __future__ import annotations

from app.terms import BOUNDARY_STOP, extract_terms, parse_clause_refs


def test_跨词边界的伪词不得被造出来():
    """字级滑窗会切出中文里根本不存在的词，而它们照样能 `ilike` 命中条款正文。

    踩过一次：问重金属的一句被切出 `包装重`——它在提问里跨「包装|重金属」，
    在另一条讲重复使用的条款标题里跨「包装|重复」，两边都不是词、字面却一致，
    于是把一条毫不相干的条款顶过了冲突闸门。分词之后这类词根本不进来。
    """
    t = extract_terms("我的包装重金属超标了会怎样？")
    assert "包装重" not in t and "装重" not in t
    assert {"包装", "重金属", "超标"} <= set(t)


def test_长词权重更高():
    """「受关注物质」比「物质」更能指向正确条款，靠 len ** 1.2 拉开差距。"""
    t = extract_terms("受关注物质的举证义务是什么？")
    assert t["受关注物质"] > t["物质"]


def test_虚词按词判定不按字():
    """按首尾**字**剪会误伤领域词——「可回收性」「可重复使用」都以 `可` 开头。"""
    assert "可" in BOUNDARY_STOP           # 它确实是虚词字
    assert "可回收性" in extract_terms("可回收性的评级标准是什么？")
    assert extract_terms("这是什么？怎么样？多少？") == {}


def test_停用词按整词剪():
    """`PPWR` 丢，`PPWR 第 67(5) 条` 不丢。整词判定，不看片段。"""
    assert "PPWR" not in extract_terms("PPWR 什么时候开始管？")


def test_条款号精确定位():
    refs = parse_clause_refs("第 5(4) 条和附件 II 表 3 说的是什么？")
    assert any(r.article == "5" and r.paragraph == "4" for r in refs)
