"""引用怎么呈现给用户。**一个字都不自己发明**——格式与位阶文案都是照抄的。"""

from __future__ import annotations

import pytest

from app.display import authority_note, display


@pytest.mark.parametrize("cid,want", [
    ("ppwr/art/5/4", "第 5(4) 条"),
    ("ppwr/art/5/5/c", "第 5(5)(c) 条"),
    ("ppwr/art/71", "第 71 条"),
    ("ppwr/annex/VII/2", "附件 VII 第 2 点"),
    ("ppwr/annex/II/table3", "附件 II 表 3"),
    ("ppwr/annex/IX/B/1", "附件 IX B 部分第 1 点"),
    ("ppwr_guidance/sec/19", "指南第 19 节"),
    ("ppwr_faq/ch/III/q/17", "FAQ 第 III 章问 17"),
    ("dec_2026_429/art/1", "Delegated Decision (EU) 2026/429 第 1 条"),
])
def test_引用串按格式派生(cid, want):
    assert display(cid) == want


def test_位阶34必须带无约束力标注():
    """红线：不得把官方指南、FAQ 与上位法规呈现为同等效力。"""
    assert "无法律约束力" not in authority_note(1)
    assert "无法律约束力" in authority_note(3)
    assert "仅反映作者观点" in authority_note(4)
