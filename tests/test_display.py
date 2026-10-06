"""引用怎么呈现给用户。**一个字都不自己发明**——格式与位阶文案都是照抄的。

只用演示切片里有的条款 id；项、附件、表这几种形态的样例在 `tests/full/test_display_full.py`
（那几条是切片外的真实条款，公开仓不发布，仓库迁移方案第 6.3 节）。
"""

from __future__ import annotations

import pytest

from app.display import authority_note, display


@pytest.mark.parametrize("cid,want", [
    ("ppwr/art/5/4", "第 5(4) 条"),
    ("ppwr/art/70/3", "第 70(3) 条"),
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
