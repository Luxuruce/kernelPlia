"""引用的呈现：`display` 字符串与位阶标注。

**这一层的输出全部是用户可见的**，所以措辞不是开发能定的：
`display` 的格式在 `CLAUDE.md`「引用格式」一节，
`authority_note` 的四段文案在 `knowledge/ppwr/copy_disclaimer.md` 第 2 节。
本模块只做**派生**，一个字都不自己发明；格式没覆盖的形态见 UNSPECIFIED_FORMS。
"""

from __future__ import annotations

import re

# copy_disclaimer.md 第 2 节「效力位阶标注（按引用来源自动附加）」。
# 由 authority_rank 决定，**服务端补上**（产品契约文档 第三节校验 4），逐字照抄。
AUTHORITY_NOTE = {
    1: "有法律约束力",
    2: "有法律约束力（授权法案，可增设法规正文没有的豁免）",
    3: "委员会解释，无法律约束力；成员国与法院会予以重视",
    4: "委员会解释，无法律约束力；官方明示仅反映作者观点",
}

# CLAUDE.md「引用格式」给了四种形态：
#   法规 第 7(1)(a) 条 · 附件 V 第 2 点 · 指南第 19 节 · FAQ 第 X 章问 5
#   授权法案 Delegated Decision (EU) 2026/429 第 1 条
DELEGATED_ACT_NAME = {
    "dec_2026_429": "Delegated Decision (EU) 2026/429",
}

# 无编号自然段的语义标识（README 第 3 节：用语义不用序号，修正案插段时序号会移位）。
# 这几个的 display 格式**引用格式一节没有覆盖**，见 UNSPECIFIED_FORMS。
SEMANTIC_POINT = {
    "def_pfas": "PFAS 定义段",
    "rev_2030": "2030 年评估段",
    "ms_2025": "成员国信息提交段",
}

#: 引用格式规范没有覆盖、由开发侧暂定的三种形态。**已列入《待决项清单》待产品确认。**
UNSPECIFIED_FORMS = (
    "指南被切分的子段（`<指南>/sec/<节>/<子段>`）",
    "法条无编号自然段（`<法规>/art/<条>/<款>/<语义标识>`）",
    "署名断言（ppwr/inference/...）",
)


def authority_note(rank: int | None) -> str | None:
    return AUTHORITY_NOTE.get(rank) if rank else None


def display(clause_id: str, *, annex: str | None = None, heading: str | None = None) -> str:
    """由稳定标识派生用户可见的引用串。"""
    # 授权法案：Delegated Decision (EU) 2026/429 第 1 条
    m = re.fullmatch(r"([a-z0-9_]+)/art/(\d+)", clause_id)
    if m and m.group(1) in DELEGATED_ACT_NAME:
        return f"{DELEGATED_ACT_NAME[m.group(1)]} 第 {m.group(2)} 条"

    # 附件。`annex` 实际有三种形态，引用格式规范（CLAUDE.md）
    # 对前两种给了写法，第三种按同样的构词法推：
    #   VII/2      → 附件 VII 第 2 点
    #   II/table3  → 附件 II 表 3          ← 规范原文就是「附件 II 表 3」
    #   IX/B/1     → 附件 IX B 部分第 1 点   ← 分部分的附件
    m = re.fullmatch(r"ppwr/annex/([IVXLC]+)(?:/(.+))?", clause_id)
    tail = m.group(2) if m else (annex.partition("/")[2] if annex else None)
    head = m.group(1) if m else (annex.partition("/")[0] if annex else None)
    if head:
        if not tail:
            return f"附件 {head}"
        t = re.fullmatch(r"table(\d+)", tail)
        if t:
            return f"附件 {head} 表 {t.group(1)}"
        part = re.fullmatch(r"([A-Z])/(\w+)", tail)
        if part:
            return f"附件 {head} {part.group(1)} 部分第 {part.group(2)} 点"
        if re.fullmatch(r"[A-Z]", tail):
            return f"附件 {head} {tail} 部分"
        return f"附件 {head} 第 {tail} 点"

    # 指南：指南第 19 节（切分的子段附一个限定词，见 UNSPECIFIED_FORMS）
    m = re.fullmatch(r"ppwr_guidance/sec/(\d+)(?:/(\w+))?", clause_id)
    if m:
        seg = {"steps": "三步法", "market": "投放市场时点"}.get(m.group(2) or "")
        return f"指南第 {m.group(1)} 节" + (f"（{seg}）" if seg else "")

    # FAQ：FAQ 第 III 章问 14
    m = re.fullmatch(r"ppwr_faq/ch/([IVXLC]+)/q/(\d+)", clause_id)
    if m:
        return f"FAQ 第 {m.group(1)} 章问 {m.group(2)}"

    # 署名断言：官方文件未覆盖，没有条款号可引（见 UNSPECIFIED_FORMS）
    if clause_id.startswith("ppwr/inference/"):
        return f"署名断言：{heading}" if heading else "署名断言"

    # 法规条款：第 5(5)(c) 条 / 第 5(4) 条 / 第 71 条
    m = re.fullmatch(r"ppwr/art/(\d+)(?:/(\w+))?(?:/(\w+))?", clause_id)
    if m:
        art, para, point = m.groups()
        out = f"第 {art}"
        if para:
            out += f"({para})"
        if point:
            # 语义标识不能当序号渲染成「(def_pfas)」
            if point in SEMANTIC_POINT:
                return f"{out} 条{SEMANTIC_POINT[point]}"
            out += f"({point})"
        return out + " 条"

    return clause_id
