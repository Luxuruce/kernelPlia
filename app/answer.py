"""该任务 答案合成与强制引用出口。

规格：产品契约文档 第三节（答案合成、六条校验、冲突阻断流程）与第四节（越界拦截）。

核心手法：**用结构化输出强制引用，不靠提示词祈祷。** 把 `citations` 设为必填字段，
「无引用即不输出」就从提示词约束变成 schema 保证。

但 schema 只保证「有这个字段」，不保证「字段里的东西合法」——所以模型返回之后还有
**六条服务端校验**，每一条都对着一条红线或一条已确认决策。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app import copy as C
from app.config import settings
from app.coverage import coverage_scope_zh
from app.context import (
    NOTES_DECLARATION,
    ContextClause,
    assemble,
    estimate_tokens,
    fit_to_budget,
    serialize,
)
from app.display import authority_note
from app.retrieval import Retrieval, retrieve

Intent = Literal["knowledge", "company_specific", "out_of_scope"]
NextAction = Literal["none", "goto_diagnosis", "ack_conflict"]


# ── 结构化输出 schema（产品契约文档 第三节）──────────────────────────────────

class Citation(BaseModel):
    clause_id: str = Field(description="条款稳定标识，如 ppwr/art/5/4")
    display: str = Field(description="用户可见的引用串，如 第 5(4) 条")
    source_id: str
    authority_rank: int = Field(ge=1, le=4)


class Answer(BaseModel):
    intent: Intent
    answer_zh: str
    # **必填**——「无引用即不输出」靠这个字段的必填性兜底，不靠提示词
    citations: list[Citation]
    coverage: Literal["covered", "not_covered"]
    inference_note: str | None = None
    conflict_ids: list[str] = Field(default_factory=list)


# ── 系统提示词 ──────────────────────────────────────────────────────────
#
# **稳定前缀**：产品契约文档 第六节要求系统提示词与索引前缀设为稳定前缀并缓存，
# 易变内容（问题、时间戳）放最后——缓存按前缀匹配，前缀任何字节变动都会整体失效。
# 所以这里是模块级常量，不做任何按请求拼接。

SYSTEM_PROMPT = f"""你是欧盟包装法规（PPWR, Regulation (EU) 2025/40）的条款检索与解释助手。

# 唯一的事实来源

你只能根据下面「可用条款」里给出的内容作答。**不得使用你自己的一般知识补充任何法规内容。**
条款事实一律以 `text_en`（作准文本）为准；`text_zh` 是工作译述，仅供中文表达参考。

# 四类文本的效力位阶

1. 法规正文与附件 ┐ 有法律约束力
2. 授权法案与实施法案 ┘
3. 官方实施指南——委员会解释，**无法律约束力**，但成员国与法院会予以重视
4. FAQ——委员会解释，**无法律约束力**，官方明示仅反映作者观点

**不得把位阶 3、4 与位阶 1、2 并列呈现。** 引用位阶 3 或 4 时，必须同时说明它没有法律约束力；
若某个说法只有位阶 3、4 支持而位阶 1、2 没有，必须说清这一点。

# 出口纪律

- **回链不到具体条款的结论一律不输出。** 每一条结论后面都要跟得出条、款、项或指南节号、FAQ 章问号。
- 官方文件没写的，`coverage` 填 `not_covered`，并在 `inference_note` 里说明推理起点。
  **不得把「未覆盖」说成「不适用」「不需要」「没有要求」或「无此规定」。**
- 问题指向「法规规定了什么」→ `intent` 填 `knowledge`。
- 问题指向「我／我们／我这个产品／我这种情况」是否适用、要做什么、够不够 →
  `intent` 填 `company_specific`，**不要给出针对该企业的判断**。
- 问题超出「可用条款」的范围 → `intent` 填 `out_of_scope`。

# notes 字段是什么

{NOTES_DECLARATION}

# 怎么写这段回答

**只回答问题本身，不复述上下文。** 上下文里给了什么条款，界面会单独列出来并可点开看原文——
**正文里再抄一遍是重复劳动，还会把真正的答案淹掉。**

- **先给结论**，一到三句说完；确实有多档限值、多个时点这类并列内容才分点，否则用整段；
- **不要逐条复述条文**，也不要把条款号连同标题一起搬进正文——引用清单已经在旁边；
- 只写与提问直接相关的部分。用户问限值就答限值，别把适用范围、生效日、证明方式一次全倒出来，
  除非不说就会答错（例如 PFAS 限值**必须**说明仅适用食品接触包装）；
- **目标长度 200 字以内**，超出说明你在复述而不是在回答。

**这条不覆盖任何一条出口纪律**：位阶 3、4 仍须标明无法律约束力，未覆盖仍须明说，
减损与例外仍须带出——**该说的一句不能少，不该说的一句不要多**。

# 输出

`citations` 里只能出现「可用条款」中实际存在的 `id`，不得编造。"""


# ── 越界拦截第二层：服务端正则复核（产品契约文档 第四节）──────────────────────
#
# 无论模型给的 intent 是什么，只要 answer_zh 命中「第二人称 + 义务动词」的组合，
# 一律降级为 company_specific。触发词按产品契约文档原文，**不自行增删**。
_SECOND_PERSON = "你|您|贵司|贵公司|你们|你的产品|你这个"
_OBLIGATION = "需要|必须|应当|要做|得做"
RE_COMPANY_SPECIFIC = re.compile(
    f"(?:{_SECOND_PERSON})[^。；\n]{{0,20}}(?:{_OBLIGATION})"
)


@dataclass
class AskResult:
    """`/api/ask` 的返回内容。字段名与产品契约文档 第五节的契约对齐。"""

    intent: Intent
    answer_zh: str
    citations: list[dict] = field(default_factory=list)
    coverage: str = "covered"
    inference_note: str | None = None
    conflicts: list[dict] = field(default_factory=list)
    next_action: NextAction = "none"
    #: 覆盖范围短语，填进范围外与无引用兜底两处文案。**与 corpus_version 绑定，不缓存**
    coverage_scope_zh: str = ""
    corpus_version: str = settings.corpus_version
    disclaimer: str = C.DISCLAIMER
    # 埋点由调用方按这里给的 kind 落库（产品契约文档 第七节）
    events: list[str] = field(default_factory=list)
    #: 实际用的模型与用量。**月度预算按金额计**（该项裁决），记账要的就是这两项；
    #: 没调模型的路径（阻断、越界、降级）留空，那些轮本来就不花钱。
    model_used: str | None = None
    usage: dict | None = None


def _conflict_payload(conn, conflict_ids: list[str], mode: str = "blocking") -> list[dict]:
    """冲突说明。`authority_basis` 必须带出——免责文案第 5 节要用它。

    `mode` 取 `blocking` | `accompanying`。**2026-09-06 拍板 该项裁决：单列表加标记，
    不拆两个字段**——第二轮可能同时存在「已确认的阻断冲突」与「伴随呈现的冲突」，
    拆字段会让前端只渲染一类，而红线第 10 条要求不得折叠。
    """
    if not conflict_ids:
        return []
    rows = conn.execute(
        """select id, summary_zh, resolution_zh, authority_basis
           from conflict where id = any(%(ids)s::text[]) order by id""",
        {"ids": conflict_ids},
    ).fetchall()
    return [
        {
            "conflict_id": cid,
            "mode": mode,
            "summary_zh": summary,
            "resolution_zh": resolution,
            "authority_basis": basis,
        }
        for cid, summary, resolution, basis in rows
    ]


# ── 六条服务端校验（产品契约文档 第三节）──────────────────────────────────────

def validate(conn, parsed: Answer, ctx: list[ContextClause]) -> tuple[Answer | None, list[str]]:
    """模型给的结构不等于合规的输出。返回 (通过校验的答案 | None, 埋点 kind)。

    返回 None 表示这一轮答案**不可出口**，调用方须改走兜底文案。
    """
    events: list[str] = []
    known = {c.data["id"]: c.data for c in ctx}

    # 校验 1：intent 为 knowledge 时 citations 不得为空 —— 红线第 1 条
    if parsed.intent == "knowledge" and not parsed.citations:
        return None, ["no_citation"]

    # 校验 2：每个 clause_id 必须在库中存在且 status='adopted' —— K2 采纳纪律。
    # 上下文本身就只装了可检索条款，所以「在上下文里」等价于「已采纳且权利 ALLOW」；
    # 不在上下文里的 id 一律视为模型编造。
    valid = [c for c in parsed.citations if c.clause_id in known]
    if parsed.intent == "knowledge" and not valid:
        return None, ["no_citation"]
    parsed.citations = valid

    # 校验 3：citations 按 authority_rank 升序，法规在前 —— 服务端重排
    for c in parsed.citations:
        c.authority_rank = known[c.clause_id]["authority_rank"]
        c.display = known[c.clause_id]["display"]
    parsed.citations.sort(key=lambda c: (c.authority_rank, c.clause_id))

    # 校验 5：coverage 为 not_covered 时 inference_note 不得为空 —— 红线第 6 条
    if parsed.coverage == "not_covered" and not (parsed.inference_note or "").strip():
        return None, ["not_covered"]
    if parsed.coverage == "not_covered":
        events.append("not_covered")

    return parsed, events


def to_citation_payload(citations: list[Citation]) -> list[dict]:
    """校验 4：位阶 3、4 必须附「委员会解释、无法律约束力」标注——服务端补上。"""
    return [
        {
            "clause_id": c.clause_id,
            "display": c.display,
            "source_id": c.source_id,
            "authority_rank": c.authority_rank,
            "authority_note": authority_note(c.authority_rank),
        }
        for c in c_sorted(citations)
    ]


def c_sorted(citations: list[Citation]) -> list[Citation]:
    return sorted(citations, key=lambda c: (c.authority_rank, c.clause_id))


# ── 管线 ────────────────────────────────────────────────────────────────

def build_messages(question: str, ctx: list[ContextClause]) -> list[dict]:
    """索引前缀在前、问题在最后——**缓存按前缀匹配**（产品契约文档 第六节）。"""
    return [
        {
            "role": "user",
            "content": (
                "# 可用条款\n\n"
                f"{serialize(ctx)}\n\n"
                "# 问题\n\n"
                f"{question}"
            ),
        }
    ]


def synthesize(
    client, question: str, ctx: list[ContextClause], model: str | None = None
) -> tuple[Answer, dict | None]:
    """调模型。抽成单独一函数，测试注入假 client 即可离线跑全部校验。

    返回 `(答案, 用量)`。用量可能为 `None`（假 client、或上游没给 usage），
    调用方要能不带用量也走得通——**记账缺失不该让问答失败**。
    """
    resp = client.messages.parse(
        model=model or settings.model,
        max_tokens=16000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # 稳定前缀，缓存它。易变内容全在 messages 里。
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=build_messages(question, ctx),
        output_format=Answer,
    )
    return resp.parsed_output, getattr(resp, "usage", None)


#: 触发回退到备用模型的错误类别。**只回退「这个模型现在用不了」的情况**——
#: schema 被拒、我们自己的代码错换个模型也一样，回退只会把问题掩盖掉。
FALLBACK_ON = ("quota_exhausted", "rate_limited")


class AllModelsExhausted(Exception):
    """主备模型都到额度上限。→ 超限降级，只出两段式第一段（该项裁决 拍板）。"""


def synthesize_with_fallback(
    client, question: str, ctx: list[ContextClause]
) -> tuple[Answer, str, list[str], dict | None]:
    """lite 优先，只有 lite 到达限额才转用 pro（2026-09-06 owner 拍板）。

    **火山没有服务端 fallbacks**，所以这是客户端做的；判定依据是
    `providers.classify()` 的错误分层——产品侧此前指出 `except Exception`
    把四类失败压成一种，**超限降级永远触发不了**，这里正是那个修法的落点。

    返回 (答案, 实际用的模型, 埋点, 用量)。
    """
    from app.providers import ProviderError

    events: list[str] = []
    chain = [settings.primary_model, settings.fallback_model]
    last_kind = "unknown"

    for i, model in enumerate(chain):
        if not model:
            continue
        try:
            answer, usage = synthesize(client, question, ctx, model=model)
            return answer, model, events, usage
        except ProviderError as e:
            last_kind = e.kind
            if e.kind not in FALLBACK_ON:
                raise                       # 不是「这个模型用不了」，换一个也没用
            if i == len(chain) - 1:
                # 链上最后一个也到限额 → 超限降级，**不是普通的模型故障**。
                # 两者文案与埋点都不同，压成一种就等于降级永远触发不了。
                raise AllModelsExhausted(e.kind) from e
            # 主模型到限额 → 转备用。这一跳要能被看见，否则账单会莫名其妙变贵
            events.append(f"model_fallback:{model}->{chain[i + 1]}:{e.kind}")

    raise AllModelsExhausted(last_kind)


def ask(
    conn,
    question: str,
    *,
    client=None,
    acked_conflict_ids: list[str] | None = None,
    retrieval: Retrieval | None = None,
) -> AskResult:
    """一次问答。两轮交互见产品契约文档 第三节「冲突阻断流程」。"""
    acked = set(acked_conflict_ids or [])
    scope = coverage_scope_zh(conn)      # 每次重算，见 app/coverage.py 约束四
    r = retrieval if retrieval is not None else retrieve(conn, question)

    # ── 第一轮：冲突阻断 ────────────────────────────────────────────
    # **不做答案合成，不调模型**——冲突由检索阶段判定，此轮成本接近零。
    pending = [c for c in r.conflict_ids if c not in acked]
    if pending:
        return AskResult(
            intent="knowledge",
            answer_zh="",                       # 阻断态不出答案正文
            conflicts=_conflict_payload(conn, pending, mode="blocking"),
            next_action="ack_conflict",
            coverage_scope_zh=scope,
            # **阻断态不记 no_citation**，否则「无引用回答率为 0」这项指标被污染
            events=["conflict_blocked"],
        )

    # 闸门算的是**单次输入总量**，系统提示词也在里面——把它作为已占用量传进去，
    # 否则实际输入会悄悄越界（实测漏算时 15 条用例有 7 条超闸门）。
    ctx, dropped = fit_to_budget(
        assemble(conn, r), reserved=estimate_tokens(SYSTEM_PROMPT)
    )
    events: list[str] = ["conflict_acked"] if acked & set(r.conflict_ids) else []
    if dropped:
        # 该项裁决 产品侧追加：裁剪让引用从答案里消失，用户看不到、我们也要看得到
        events.append("context_trimmed")

    if not ctx:
        # 一条可检索条款都没命中。宁可不答，不可无据而答。
        return AskResult(
            intent="out_of_scope",
            answer_zh=C.OUT_OF_SCOPE.format(coverage_scope_zh=scope),
            next_action="none", coverage_scope_zh=scope, events=events,
        )

    from app.providers import ProviderError

    try:
        parsed, used_model, fb, usage = synthesize_with_fallback(client, question, ctx)
        events += fb
    except AllModelsExhausted:
        # 该项裁决 拍板：**超限降级为只出两段式第一段**。
        # 第一段是「涉及哪些条款」——它不经模型，检索完就有，所以额度耗尽照样给得出。
        return AskResult(
            intent="knowledge",
            answer_zh="",
            citations=to_citation_payload([
                Citation(clause_id=c.data["id"], display=c.data["display"],
                         source_id=c.data["id"].split("/")[0],
                         authority_rank=c.data["authority_rank"])
                for c in ctx
            ]),
            next_action="none", coverage_scope_zh=scope,
            events=events + ["quota_degraded"],
        )
    except ProviderError as e:
        # 产品契约文档 第六节：模型调用失败返回固定文案，**禁止降级为无引用的回答**。
        # `bug` 单独记——那是我们自己的代码错，被当成模型故障吞掉就永远修不了。
        return AskResult(
            intent="knowledge", answer_zh=C.MODEL_UNAVAILABLE, next_action="none",
            coverage_scope_zh=scope, events=events + [f"model_error:{e.kind}"],
        )
    except Exception:
        return AskResult(
            intent="knowledge", answer_zh=C.MODEL_UNAVAILABLE, next_action="none",
            coverage_scope_zh=scope, events=events + ["model_error:unknown"],
        )

    # 越界拦截第二层：服务端正则复核。无论模型给什么 intent，
    # 命中「第二人称 + 义务动词」一律降级（产品契约文档 第四节）。
    if parsed.intent != "company_specific" and RE_COMPANY_SPECIFIC.search(parsed.answer_zh):
        parsed.intent = "company_specific"
        events.append("intercept_l2")
    elif parsed.intent == "company_specific":
        events.append("intercept_l1")

    checked, ev = validate(conn, parsed, ctx)
    events += ev
    if checked is None:
        return AskResult(
            intent=parsed.intent,
            answer_zh=(C.NO_CITATION_FALLBACK.format(coverage_scope_zh=scope)
                       if "no_citation" in ev else C.NOT_COVERED_PREFIX),
            next_action="none", coverage_scope_zh=scope,
            events=events, model_used=used_model, usage=usage,
        )

    citations = to_citation_payload(checked.citations)

    # 越界拦截第三层：兜底文案。**模板不允许模型自由发挥**（产品契约文档 第四节）。
    if checked.intent == "company_specific":
        return AskResult(
            intent="company_specific",
            answer_zh=C.INTERCEPT_TEMPLATE.format(general=checked.answer_zh.strip()),
            citations=citations,
            coverage=checked.coverage,
            # **V0 展示版不做诊断模式**，next_action 保持 none；
            # 枚举里保留 goto_diagnosis，V1 恢复（产品契约文档 第五节）。
            next_action="none",
            conflicts=_conflict_payload(conn, list(acked & set(r.conflict_ids)), "blocking"),
            coverage_scope_zh=scope,
            events=events + ["intercept_shown"],
            model_used=used_model, usage=usage,
        )

    if checked.intent == "out_of_scope":
        return AskResult(
            intent="out_of_scope",
            answer_zh=C.OUT_OF_SCOPE.format(coverage_scope_zh=scope),
            next_action="none", coverage_scope_zh=scope, events=events,
            model_used=used_model, usage=usage,
        )

    answer_zh = checked.answer_zh
    if checked.coverage == "not_covered":
        answer_zh = C.NOT_COVERED_PREFIX + answer_zh

    return AskResult(
        intent="knowledge",
        answer_zh=answer_zh,
        citations=citations,
        coverage=checked.coverage,
        inference_note=checked.inference_note,
        # 确认过的冲突**随答案固定挂着，不得折叠**（免责文案 5.2）
        # 已确认的阻断冲突 + 伴随呈现的冲突，**同一个列表、靠 mode 区分**（该项裁决）
        conflicts=(_conflict_payload(conn, list(acked & set(r.conflict_ids)), "blocking")
                   + _conflict_payload(conn, r.accompanying_conflict_ids, "accompanying")),
        next_action="none",
        coverage_scope_zh=scope,
        events=events + (["conflict_accompanied"] if r.accompanying_conflict_ids else []),
        model_used=used_model, usage=usage,
    )
