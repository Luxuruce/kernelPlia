"""上下文装配：检索到的条款怎么进模型。

规格出自 产品契约文档 第三节「检索到的条款怎么进上下文」。

> **字段集与「哪些不进」是产品侧定的，不要自行增删。**
> 序列化格式由开发定。

所以本模块的字段清单逐字照搬那张表，另有一条守卫 `assert_field_set` 在测试里盯着——
少一个字段是功能缺失，多一个字段是白烧 token 且可能泄露内部口径（`bbox` 一个单元就 1 KB）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import settings
from app.display import authority_note, display
from app.retrieval import Retrieval

#: 进上下文的字段。**产品侧定的，改动须回产品审核。**
CONTEXT_FIELDS = (
    "id", "display", "authority_rank",
    "text_en", "text_zh",
    "scope", "effective_expr", "numeric_limits",
    "pending_dependency",
    "notes",
    "is_inference", "adopted_by",
)

#: 明确**不进**上下文的字段。回溯与高亮用，模型不需要，纯占 token。
EXCLUDED_FIELDS = ("bbox", "page_image_urls", "line_from", "line_to")

#: 系统提示词里声明 `notes` 是什么。**措辞固定**（产品契约文档 第三节 notes 处置第二条），
#: 逐字照抄，不得改写——改了就等于改产品定的模型约束。
NOTES_DECLARATION = (
    "`notes` 是答题纪律与维护注记，**不是条文内容**。"
    "据它调整回答的取舍与措辞，**不得引用它、不得把它的措辞改写进答案正文**。"
    "条文事实一律以 `text_en` 为准。"
)

# 装配时按这个顺序取列。`display` 由 id 派生，不在库里。
_SQL_COLS = (
    "id", "annex", "heading", "text_en", "text_zh", "scope",
    "effective_expr", "numeric_limits", "pending_dependency", "notes",
    "is_inference", "adopted_by",
)


@dataclass
class ContextClause:
    data: dict
    origin: str
    via: str | None = None


def assemble(conn, retrieval: Retrieval) -> list[ContextClause]:
    """把命中集装成上下文条目，按位阶升序（法规在前，规范 5.2、红线第 10 条）。"""
    if not retrieval.hits:
        return []

    ids = [h.clause_id for h in retrieval.hits]
    rows = conn.execute(
        f"""
        select {', '.join('c.' + c for c in _SQL_COLS)}, s.authority_rank
        from clause c join source s on s.id = c.source_id
        where c.id = any(%(ids)s::text[])
          and c.status = 'adopted' and s.rights_status = 'ALLOW'
        """,
        {"ids": ids},
    ).fetchall()

    by_id = {r[0]: r for r in rows}
    out: list[ContextClause] = []
    for hit in retrieval.hits:
        row = by_id.get(hit.clause_id)
        if row is None:
            continue                      # 采纳闸门或权利闸挡下的，不进上下文
        rec = dict(zip(_SQL_COLS + ("authority_rank",), row))

        item: dict = {
            "id": rec["id"],
            "display": display(rec["id"], annex=rec["annex"], heading=rec["heading"]),
            "authority_rank": rec["authority_rank"],
            "text_en": rec["text_en"],
            "scope": rec["scope"],
            "is_inference": rec["is_inference"],
            "adopted_by": rec["adopted_by"],
        }
        # text_zh 为空的单元只给 text_en（产品契约文档 第三节该行说明）
        if rec["text_zh"]:
            item["text_zh"] = rec["text_zh"]
        for k in ("effective_expr", "numeric_limits"):
            if rec[k]:
                item[k] = rec[k]
        # pending_dependency 有值时才进
        if rec["pending_dependency"]:
            item["pending_dependency"] = rec["pending_dependency"]
        if rec["notes"]:
            item["notes"] = rec["notes"]

        out.append(ContextClause(data=item, origin=hit.origin, via=hit.via))

    # 位阶升序：法规与附件在前，指南与 FAQ 在后。
    # 服务端重排是校验 3 的要求，在装配阶段就排好，模型看到的顺序即最终顺序。
    out.sort(key=lambda c: (c.data["authority_rank"], c.data["id"]))
    return out


def serialize(items: list[ContextClause]) -> str:
    """序列化格式由开发定。用 JSON——字段边界清楚，模型不会把 notes 当条文读。"""
    return json.dumps([i.data for i in items], ensure_ascii=False, indent=1)


# ── token 预算 ──────────────────────────────────────────────────────────
#
# 产品契约文档 第六节：单次提问输入 token 上限 **15 000**，超出则先缩小召回范围再调用。
# 这是成本闸门，是引流产品的生死线——**调整须回产品审核**。
#
# 2026-09-06 产品由 10 000 放宽到 15 000：闸门刻度原来是按 Opus 5 的 ¥0.49/问 定的，
# 换到 lite（实测 ¥0.011/问）之后，10K→15K 的单问成本差是 **一厘五毫**，
# 而这一厘五毫正在换掉条款级引用本身。**放宽只降低发作频率，顺序修复才是真修复**
# （见下方 该项裁决 两条附加约束）。

#: **闸门只有一个出处**——此前 `settings.max_input_tokens` 与这里各写一份、
#: 谁也没用谁，改一处另一处不动，正是「复述就会漂」的形态。
TOKEN_BUDGET = settings.max_input_tokens


def estimate_tokens(text: str) -> int:
    """**估算**，不是实测。

    精确计数要调 Anthropic 的 count_tokens 接口，本机没有 API key，
    所以这里用一个保守的经验系数：中日韩字符按 1 token，其余按 4 字符 1 token。
    实测口径的数字必须等 key 到位后用 SDK 重跑——**不要拿这个数去改闸门**。
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    return cjk + (len(text) - cjk + 3) // 4


# 超预算时的丢弃优先级：数字越小越保得住。
#
# 产品契约文档 第六节只说「超出则先缩小召回范围再调用」，没定丢弃顺序。
# 下面这个次序是按各类命中「丢了会不会答错」排的，**已列入《待决项清单》待确认**——
# 它决定哪些引用会从答案里消失，是用户可见的。
#: 丢弃优先级。**不是按「谁进来得早」排，是按「丢了会不会答错、会不会踩红线」排。**
#:
#: 0  冲突关联        缺一条，分级呈现就不完整——红线第 10 条
#: 1  explains 上扩   缺了答案只剩位阶 3/4、拿不出法条依据——红线第 10 条（该项改动 的全部理由）
#: 2  必扩三类        减损是规则库一等公民，缺了是**错答不是漏答**（决策 #13、规范 6.5）
#: 3  条号精确命中    用户点名要的
#: 4  主题词命中      按相关度进来的
#: 5  向下 explains / requires   对已有条款的补充说明，最先丢
_MUST_VIA = ("derogates", "amends", "depends_on_pending")


def _base_rank(item: ContextClause) -> int:
    origin, via = item.origin, item.via or ""
    if origin == "conflict_related":
        return 0
    if origin == "expanded" and "上扩" in via:
        return 1
    if origin == "expanded" and any(t in via for t in _MUST_VIA):
        return 2
    if origin == "exact":
        return 3
    if origin == "topic":
        return 4
    return 5


def _keep_rank(item: ContextClause) -> tuple:
    # 同档内位阶低的先丢：位阶 4 的 FAQ 比位阶 1 的法条更可牺牲
    return (_base_rank(item), item.data["authority_rank"], item.data["id"])


def _upward_source(item: ContextClause) -> str | None:
    """上扩条款的来源 id。`via` 形如 `<来源 id> --explains(上扩)-->`。"""
    via = item.via or ""
    if item.origin != "expanded" or "上扩" not in via:
        return None
    return via.split(" --", 1)[0]


def plan_trim(items: list[ContextClause]) -> tuple[list[tuple], dict[str, str]]:
    """按 该项裁决 的两条附加约束排出丢弃次序，并给出原子绑定关系。

    返回 `(排好序的 (key, ids) 列表, {成员 id: 单元代表 id})`。

    **一、派生不得优先于来源**（该项裁决）。上表的档位只区分类别，不区分
    「派生与来源同时在场」和「来源已经被丢掉」。做法是把派生的排序键
    直接挂在来源键之后——同档同位阶时来源必然排在派生之前，
    且派生**不可能**比来源先被保住。

    **二、位阶锚点与它的解释同进同出**（该项裁决）。一条 rank 3/4 的解释与它上扩出来的
    rank 1/2 法条是裁剪意义上不可分割的一个单元：只改排序的话，切口正好落在
    两者之间时仍会留下一条没有法条依据的解释——那是红线第 10 条。

    **两条缺一不可。** 只绑定，倒挂本身没修；只排序，切口仍可能落在中间。
    """
    by_id = {i.data["id"]: i for i in items}

    # 原子单元：rank 3/4 的解释 ←→ 它上扩出来的 rank 1/2 法条
    rep: dict[str, str] = {i.data["id"]: i.data["id"] for i in items}
    for i in items:
        src = _upward_source(i)
        if src is None or src not in by_id:
            continue
        if by_id[src].data["authority_rank"] >= 3 and i.data["authority_rank"] <= 2:
            rep[i.data["id"]] = rep[src]

    def chain_key(item: ContextClause, depth: int = 0) -> tuple:
        """派生的键 = 来源的键 + 一段后缀，从而永远紧跟在来源之后。"""
        src = _upward_source(item)
        if src is not None and src in by_id and depth < 4:
            return chain_key(by_id[src], depth + 1) + (1, item.data["id"])
        return _keep_rank(item)

    units: dict[str, list[ContextClause]] = {}
    for i in items:
        units.setdefault(rep[i.data["id"]], []).append(i)

    # 单元整体按其**最该保住**的那个成员定位——绑定的意义就是让锚点抬着解释一起留下
    ordered = sorted(
        ((min(chain_key(m) for m in ms), [m.data["id"] for m in ms]) for ms in units.values()),
    )
    return ordered, rep


def fit_to_budget(
    items: list[ContextClause], budget: int = TOKEN_BUDGET, reserved: int = 0
) -> tuple[list[ContextClause], list[str]]:
    """把上下文压进成本闸门。返回 (保留项, 被丢弃的 clause_id)。

    产品契约文档 第六节：**超出则先缩小召回范围再调用**。10 000 是成本闸门，
    是引流产品的生死线，**调整须回产品审核**——所以这里只压不改闸门。

    `reserved` 是同一次请求里**上下文之外**已经占掉的输入 token——目前就是系统提示词。
    闸门管的是「单次提问输入 token 上限」，**系统提示词也在里面**；
    只按上下文算会让实际输入悄悄超出（实测漏算时 15 条用例有 7 条越界）。
    该项裁决 把缓存对象改为「稳定指令层」后系统提示词还会变长，这个口径更要守住。

    丢弃顺序见 KEEP_PRIORITY。被丢的 id 要进埋点 `context_trimmed`：
    裁剪让引用从答案里消失，**用户看不到、我们也要看得到**。
    """
    room = budget - reserved
    if estimate_tokens(serialize(items)) <= room:
        return items, []

    by_id = {i.data["id"]: i for i in items}
    order, _ = plan_trim(items)
    keep_ids = [cid for _, ids in order for cid in ids]
    # **按单元丢**，不按条丢：绑定的两条要么都在、要么都不在（该项裁决 第二条）
    dropped: list[str] = []
    while order and estimate_tokens(
        serialize([by_id[c] for c in keep_ids])
    ) > room:
        _, ids = order.pop()
        for cid in ids:
            keep_ids.remove(cid)
            dropped.append(cid)

    kept = [by_id[c] for c in keep_ids]
    kept.sort(key=lambda c: (c.data["authority_rank"], c.data["id"]))
    return kept, dropped


def budget_report(items: list[ContextClause]) -> dict:
    """按闸门口径给一份负载明细，供 该项裁决 埋点与成本闸门监控用。"""
    body = serialize(items)
    notes_chars = sum(len(i.data.get("notes") or "") for i in items)
    notes_only = "".join(i.data.get("notes") or "" for i in items)
    total = estimate_tokens(body)
    return {
        "clauses": len(items),
        "chars": len(body),
        "est_tokens": total,
        "notes_est_tokens": estimate_tokens(notes_only),
        "notes_share": round(estimate_tokens(notes_only) / total, 3) if total else 0.0,
        "notes_chars": notes_chars,
        "over_budget": total > TOKEN_BUDGET,
    }
