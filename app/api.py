"""HTTP 接口。

契约在 产品契约文档 第五节。本文件目前只实现 该任务 需要的 `/api/clause/{clause_id}`；
`/api/ask` 与 `/api/lead` 待 该任务 与 C2。

    ~/.venvs/kernel/bin/uvicorn app.api:app --reload
"""

from __future__ import annotations

import pathlib
import random
import time
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.answer import ask
from app.config import PAGE_IMAGE_DIR, PAGE_IMAGE_URL_PREFIX, settings
from app.context import assemble, estimate_tokens, fit_to_budget
from app.coverage import coverage_scope_zh
from app.db import connect
from app.display import authority_note, display
from app import copy as C
from app import limits
from app.providers import make_client
from app.retrieval import retrieve

app = FastAPI(title="合规内核 V0", version=settings.corpus_version)

# 模型客户端建一次就够，别每次请求重建
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = make_client()
    return _CLIENT


def _serialize_for_count(ctx):
    from app.context import serialize
    return serialize(ctx)


from app.answer import SYSTEM_PROMPT  # noqa: E402

#: 闸门算的是单次输入总量，系统提示词也在里面
_SYSTEM_TOKENS = estimate_tokens(SYSTEM_PROMPT)

# 前端与后端**同源**：一个源、不需要 CORS、页图与页面同域。
# 展示版的量级不需要 CDN，真正的瓶颈是模型那 20–40 秒（见《待决项清单》该项裁决）。
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


# 页图静态托管。**运行时不做渲染**（产品契约文档 第二节）——
# 232 页由 scripts/render_pages.py 一次性预渲染好。
PAGE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    PAGE_IMAGE_URL_PREFIX,
    StaticFiles(directory=PAGE_IMAGE_DIR),
    name="page_images",
)


def page_image_urls(source_id: str, page_from: int | None, page_to: int | None) -> list[str]:
    """按页序给出预渲染页图的 URL。命名规则 {source_id}/p{page}.png。

    只列**实际存在**的页图：`04_dec_2026_429` 有 PDF 所以有页图，
    但没有 bbox 边车——那是两回事，前端降级的是高亮框不是页图。
    """
    if page_from is None:
        return []
    urls = []
    for p in range(page_from, (page_to or page_from) + 1):
        if (PAGE_IMAGE_DIR / source_id / f"p{p}.png").exists():
            urls.append(f"{PAGE_IMAGE_URL_PREFIX}/{source_id}/p{p}.png")
    return urls


@app.get("/api/clause/{clause_id:path}")
def get_clause(clause_id: str, session_id: str | None = Query(default=None)) -> dict:
    """该任务 引用回溯：展示原文页图像并叠加高亮框。

    契约见产品契约文档 第五节。两点纪律：

    · **不返回 `notes`** ——它是给模型的答题纪律与内部运营口径，
      暴露给用户等于把内部纪律贴到台面上。
    · 只有 `status = 'adopted'` 且来源 `rights_status = 'ALLOW'` 的条款可被读取，
      否则 404——草稿不该能通过猜 id 看到。
    """
    with connect() as conn:
        row = conn.execute(
            """
            select c.id, c.annex, c.heading, c.text_en, c.text_zh,
                   c.lang_authenticity_zh, c.page_from, c.page_to, c.bbox,
                   c.is_inference, c.adopted_by, c.adopted_at, c.source_id,
                   s.title, s.instrument_class, s.authority_rank
            from clause c join source s on s.id = c.source_id
            where c.id = %(id)s
              and c.status = 'adopted' and s.rights_status = 'ALLOW'
            """,
            {"id": clause_id},
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="条款不存在或尚未采纳")

        (cid, annex, heading, text_en, text_zh, lang_zh, page_from, page_to,
         bbox, is_inference, adopted_by, adopted_at, source_id,
         source_title, instrument_class, authority_rank) = row

        if session_id:
            # 埋点 citation_open：该项裁决 的「提问 → 点开引用」漏斗（验证 H25 差异化是否被感知）
            conn.execute(
                "insert into event (session_id, kind, clause_id) values (%s, 'citation_open', %s)",
                (session_id, cid),
            )
            conn.commit()

    return {
        "clause_id": cid,
        "display": display(cid, annex=annex, heading=heading),
        "text_en": text_en,
        "text_zh": text_zh,
        # 契约没列这一项，但 copy_disclaimer.md 第 3 节要求条款详情页在 text_zh 旁
        # 标注语言真实性档位——没有它前端渲染不出那句标注。见《待决项清单》该项裁决。
        "lang_authenticity_zh": lang_zh,
        "source_title": source_title,
        "instrument_class": instrument_class,
        "authority_rank": authority_rank,
        "authority_note": authority_note(authority_rank),
        "page_from": page_from,
        "page_to": page_to,
        "page_image_urls": page_image_urls(source_id, page_from, page_to),
        # 归一化 0..1、原点页面左上。前端按图片实际显示尺寸乘上去即可定位，
        # 换渲染分辨率不需要重算。无边车的来源给空表，前端降级为只给页码。
        "bbox": bbox or [],
        "is_inference": is_inference,
        "adopted_by": adopted_by,
        "adopted_at": adopted_at.isoformat() if adopted_at else None,
        "corpus_version": settings.corpus_version,
    }


# ── 埋点 ────────────────────────────────────────────────────────────────

def record(conn, session_id: str, kinds: list[str], *,
           input_tokens: int | None = None, latency_ms: int | None = None,
           meta: dict | None = None) -> None:
    """把这一轮的埋点落库。kind 取值见产品契约文档 第七节。

    `input_tokens` 是**成本闸门监控**的那一项（该项裁决 九项指标里最需要盯的两条之一）。
    形如 `model_fallback:a->b:kind` 的复合 kind 拆成 kind + meta，
    否则 `kind` 这一列会变成自由文本、SQL 聚合不出来。
    """
    from psycopg.types.json import Jsonb

    for k in kinds:
        base, _, detail = k.partition(":")
        m = dict(meta or {})
        if detail:
            m["detail"] = detail
        conn.execute(
            "insert into event (session_id, kind, input_tokens, latency_ms, meta) "
            "values (%s, %s, %s, %s, %s)",
            (session_id, base, input_tokens, latency_ms, Jsonb(m) if m else None),
        )
    conn.commit()


# ── /api/ask ────────────────────────────────────────────────────────────

def client_ip(req: Request) -> str:
    """取真实来源 IP。反代后面 `req.client.host` 是网关，要看 `X-Forwarded-For` 第一跳。

    **这个值可以伪造**，所以它只用于限流这种滥用防护，不用于任何鉴权。
    该项裁决 把限流的理由从「额度保命」改成「纯粹的滥用防护」，正好对得上这个强度。
    """
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    session_id: str = Field(min_length=1, max_length=128)
    #: 第二轮回传已知悉的冲突；第一轮传空（产品契约文档 第五节）
    acked_conflict_ids: list[str] = Field(default_factory=list)


def _degraded(conn, question: str, r, ctx, scope_reason: str) -> dict:
    """超限降级：只出两段式的**第一段**——条款清单 + 原文页高亮，**不调模型**。

    产品契约文档 第六节：降级态「比没上线更伤」，所以**不硬停**；
    但降级不等于白屏——第一段本来就是差异化所在，条款清单仍然有料。
    """
    return {
        "intent": "knowledge",
        "answer_zh": "",
        "citations": [
            {"clause_id": c.data["id"], "display": c.data["display"],
             "authority_rank": c.data["authority_rank"],
             "authority_note": authority_note(c.data["authority_rank"])}
            for c in ctx
        ],
        "coverage": "covered",
        "inference_note": None,
        "conflicts": [],
        "corpus_version": settings.corpus_version,
        "disclaimer": C.DISCLAIMER,
        "coverage_scope_zh": coverage_scope_zh(conn),
        "next_action": "none",
        "degraded": scope_reason,
    }


@app.post("/api/ask")
def api_ask(req: AskRequest, request: Request) -> dict:
    """知识模式问答。契约见产品契约文档 第五节。

    两轮交互：命中冲突时第一轮**不调模型**、只返回冲突说明，
    `next_action = ack_conflict`；用户确认后带 `acked_conflict_ids` 再来一轮。

    **两道闸门在调模型之前**（该项裁决）：每 IP 每日 5 次、月度预算 100%。
    两者都**不返回错误**，而是降级为只出第一段——见 `_degraded`。
    """
    t0 = time.time()
    ip = client_ip(request)
    with connect() as conn:
        r = retrieve(conn, req.question)
        ctx, _ = fit_to_budget(assemble(conn, r), reserved=_SYSTEM_TOKENS)
        n_in = estimate_tokens(_serialize_for_count(ctx)) + _SYSTEM_TOKENS

        gate = limits.check(conn, ip)
        if not gate.allowed:
            record(conn, req.session_id, ["ask", f"degraded:{gate.reason}"],
                   input_tokens=n_in, latency_ms=int((time.time() - t0) * 1000),
                   meta={"ip_used_today": gate.used_today,
                         "month_cny": round(gate.month_cny, 4)})
            return _degraded(conn, req.question, r, ctx, gate.reason)

        res = ask(conn, req.question, client=_client(),
                  acked_conflict_ids=req.acked_conflict_ids, retrieval=r)

        events = list(res.events)
        cost = None
        if res.model_used:                      # 真的调了模型才计次、才记账
            limits.consume(conn, ip)
            u = res.usage or {}
            cost = limits.record_spend(
                conn, res.model_used,
                u.get("in") or n_in, u.get("out") or 0,
            )
            if limits.check(conn, ip).warn:
                # 70% 告警：通知人、给充值留时间。**不拦请求**（该项裁决 两级不硬停）
                events.append("budget_warn")

        # 该项裁决 按会话解锁：**在本会话的回答里作为引用出现过的节点才获得名字**。
        # 逐条落库，`/api/graph` 按 session_id 反查——净泄露严格为零。
        for c in res.citations:
            conn.execute(
                "insert into event (session_id, kind, clause_id) "
                "values (%s, 'citation_shown', %s)",
                (req.session_id, c["clause_id"]),
            )

        total_ms = int((time.time() - t0) * 1000)
        record(conn, req.session_id, ["ask"] + events,
               input_tokens=n_in, latency_ms=total_ms,
               meta={"intent": res.intent, "next_action": res.next_action,
                     # 验收改为**双指标**（验收标准文档第五节第 7 条）：
                     # 只看首字节可以在产品完全不可用的情况下达标——
                     # 实测 pro 首字节 1.4 秒「达标」，之后是 244 秒思考流。
                     "total_ms": total_ms, "model": res.model_used,
                     "out_tokens": (res.usage or {}).get("out"),
                     "cost_cny": round(cost, 5) if cost is not None else None})

    return {
        "intent": res.intent,
        "answer_zh": res.answer_zh,
        "citations": res.citations,
        "coverage": res.coverage,
        "inference_note": res.inference_note,
        "conflicts": res.conflicts,
        "corpus_version": res.corpus_version,
        "disclaimer": res.disclaimer,
        "coverage_scope_zh": res.coverage_scope_zh,
        "next_action": res.next_action,
    }


@app.get("/api/scope")
def api_scope() -> dict:
    """**两段式的第一段**：不经模型，检索完就有（产品契约文档 第六节 该项裁决）。

    前端先拿它渲染「涉及哪些条款」，3 秒内可达；正文再由 `/api/ask` 非流式补上。
    先亮条款清单**本身就是差异化**——条款级引用是卖点，不是权宜之计。
    """
    with connect() as conn:
        return {
            "coverage_scope_zh": coverage_scope_zh(conn),
            "corpus_version": settings.corpus_version,
        }


@app.post("/api/ask/clauses")
def api_ask_clauses(req: AskRequest) -> dict:
    """两段式第一段的按问检索：只出条款清单，**不调模型、不计费**。

    这一段的耗时就是验收里的**首字节**（≤ 3 秒）。单独埋点，
    因为总耗时（≤ 20 秒，> 30 秒不可接受）在 `/api/ask` 那边——
    **两个指标必须分开记**：只看首字节可以在产品完全不可用的情况下达标。
    """
    t0 = time.time()
    with connect() as conn:
        r = retrieve(conn, req.question)
        ctx, _ = fit_to_budget(assemble(conn, r), reserved=_SYSTEM_TOKENS)
        ms = int((time.time() - t0) * 1000)
        record(conn, req.session_id, ["first_byte"], latency_ms=ms,
               meta={"first_byte_ms": ms, "clauses": len(ctx)})
        return {
            "citations": [
                {"clause_id": c.data["id"], "display": c.data["display"],
                 "authority_rank": c.data["authority_rank"],
                 "authority_note": authority_note(c.data["authority_rank"])}
                for c in ctx
            ],
            "will_block": bool(set(r.conflict_ids) - set(req.acked_conflict_ids)),
            "corpus_version": settings.corpus_version,
        }


#: 首屏未提问时点亮的种子节点。由两部分组成：
#:
#: 一、**示例胶囊各自回答里位阶最高的三条**——点一下胶囊就会看到，
#:     固定点亮它们的净泄露是零，变的只是「先看到」还是「点一下再看到」；
#: 二、**全库随机三分之一**（owner 2026-09-07 拍板：7 个太少）。
#:
#: > **第二部分让 该项裁决 的「净泄露严格为零」不再成立**：约 55 条条款号无条件公开。
#: > 这是 owner 明确要的，写在这里是为了别让它悄悄发生。
#:
#: **随机必须是确定性的。** 每次请求重新随机看着一样，但刷十几次页面就能把
#: 165 条全部枚举出来——那等于全开，还多绕一圈。所以拿 `corpus_version` 当种子：
#: 同一份语料下**永远是同一个三分之一**，换语料才重洗。
#:
#: > 那三句示例问题与 `web/index.html` 的 `.chip` 是**同一份文案的两处副本**。
#: > 改了胶囊要改这里，否则种子会指向没人问得出的条款。已列入《待决项清单》。
DEMO_QUESTIONS = (
    "包装里重金属最多能有多少？",
    "PFAS 的限值是多少？",
    "PPWR 什么时候开始管？",
)
SEED_PER_QUESTION = 3
SEED_RATIO = 1 / 3


@lru_cache(maxsize=1)
def seed_clause_ids() -> frozenset[str]:
    """算一次就缓存——它只随索引与语料版本变，不随请求变。"""
    out: set[str] = set()
    with connect() as conn:
        for q in DEMO_QUESTIONS:
            ctx, _ = fit_to_budget(
                assemble(conn, retrieve(conn, q)), reserved=_SYSTEM_TOKENS
            )
            out.update(c.data["id"] for c in ctx[:SEED_PER_QUESTION])

        pool = sorted(
            row[0] for row in conn.execute(
                """select c.id from clause c join source s on s.id = c.source_id
                   where c.status = 'adopted' and s.rights_status = 'ALLOW'"""
            ).fetchall()
        )
    # 固定种子 = 固定的那三分之一。**不要换成无参的 random.shuffle**——
    # 那样每次进程重启都是新的一批，重启几次照样把全库泄光。
    rng = random.Random(f"{settings.corpus_version}:graph-seed")
    rng.shuffle(pool)
    out.update(pool[: round(len(pool) * SEED_RATIO)])
    return frozenset(out)


#: 官方原件的位置。**每一条都实测打得开才登记**——
#: 编一个打不开的官方链接比不给更糟，而用户点它正是为了核对我们说得对不对。
#: 2026-09-07 逐条抓取确认（返回的是正确的文件，不是错误页）：
#:
#:   ppwr_reg       ELI → eur-lex，OJ L_202500040，另有 EN PDF 直链
#:   dec_2026_429   ELI → eur-lex，Delegated Decision (EU) 2026/429 在
#:   ppwr_guidance  OJ C_202603084「Guidance document for Regulation (EU) 2025/40」
#:   ppwr_faq       DG ENV 出版页，2026-08-03 发布的 FAQ
#:
#: 前两份的 `url` 走库里的 `eli`（内容正本维护的持久标识，优先于这里）；
#: 后两份**不是 ELI**（一个是 OJ 直链、一个是委员会出版页），只能登记在这里。
SOURCE_URL = {
    "ppwr_guidance":
        "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=OJ:C_202603084",
    "ppwr_faq":
        "https://environment.ec.europa.eu/publications/"
        "faq-packaging-and-packaging-waste-regulation-ppwr_en",
}

#: 能一步拿到文件本身的（下载按钮用）。FAQ 那页是出版页不是直链，所以不在这里。
SOURCE_PDF_URL = {
    "ppwr_reg": "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=OJ:L_202500040",
}


@app.get("/api/sources")
def api_sources() -> dict:
    """本内核依据的官方文件清单，供页脚「回到原文」用。

    **不自托管 PDF，一律指向 EUR-Lex。** 理由是纪律不是省事：
    `knowledge/ppwr/EUoffical/` 里那几份是**冻结基线**（见该目录 MANIFEST.md），
    行号被 165 个条款单元的 `line_from/line_to` 锚着，不能跟着勘误走；
    而免责文案要求「以英文原文为准」。**把一份可能落后于勘误的副本发给用户当作准件，
    正是这条纪律要防的事。** 官方站点永远是最新的，链过去才对。

    只列 `rights_status = 'ALLOW'` 的来源——权利闸对外同样成立。
    """
    with connect() as conn:
        rows = conn.execute(
            """select id, title, instrument_class, authority_rank, eli, oj_ref, version
               from source where rights_status = 'ALLOW'
               order by authority_rank, id"""
        ).fetchall()
    return {
        "sources": [
            {"id": sid, "title": title, "instrument_class": cls,
             "authority_rank": rank, "authority_note": authority_note(rank),
             # ELI 优先——它是内容正本维护的持久标识，比这里的硬编码更权威
             "url": eli or SOURCE_URL.get(sid), "pdf_url": SOURCE_PDF_URL.get(sid),
             "oj_ref": oj, "version": ver}
            for sid, title, cls, rank, eli, oj, ver in rows
        ],
        "corpus_version": settings.corpus_version,
    }


@app.get("/api/graph")
def api_graph(session_id: str | None = Query(default=None)) -> dict:
    """首屏背景用的条款关系图谱。**节点默认匿名，按会话解锁**（拍板 该项裁决）。

    只给位阶与拓扑；**一个节点在本会话的回答里作为引用出现过之后，才获得名字、
    才可悬浮可点击**。`labels[i]` 为 `null` 即未点亮。

    **为什么不全开**：165 个节点全带条款号 + 链接两端有名，等于把这份索引的
    完整目录与关系结构一次页面加载全给出去——那是 A1 63 条 + A2 102 条
    逐条具名采纳堆出来的唯一资产（「护城河是索引不是代码」这一判断 已结：护城河 = 索引，不是代码）。

    **为什么不保持关**：悬浮报名、点开看高亮原文页，是「条款级引用」这个卖点
    最直观的证明，关掉等于放弃演示价值。

    **为什么按会话解锁比静态示例集好**：净泄露严格为零（用户看到的名字，
    是他刚刚在自己那条回答的引用清单里已经看过的）；且图谱从暗态开始、
    每问一个问题亮起一块，讲的是「**我们覆盖的远比你问到的多**」，
    全开讲的是「我们一共就这些」——前者是钩子，后者是天花板。

    枚举攻击面没有变大的**前提是限流在**（每 IP 每日 5 次，见 app/limits.py）。
    两件事绑在一起，不要单独关掉其中一个。
    """
    with connect() as conn:
        nodes = conn.execute(
            """select c.id, s.authority_rank, c.annex, c.heading from clause c
               join source s on s.id = c.source_id
               where c.status = 'adopted' and s.rights_status = 'ALLOW'
               order by c.id"""
        ).fetchall()
        idx = {cid: i for i, (cid, *_) in enumerate(nodes)}
        edges = conn.execute(
            "select from_clause_id, to_clause_id, link_type from clause_link"
        ).fetchall()
    out = {
        "ranks": [r for _, r, *_ in nodes],
        "edges": [[idx[a], idx[b], t] for a, b, t in edges if a in idx and b in idx],
    }
    # 种子先亮着，会话再往上加。**图谱从「几点微光」开始，不是全黑**——
    # 全黑的第一屏看不出这是一张真实的条款关系图。
    lit: set[str] = set(seed_clause_ids())
    if session_id:
        with connect() as conn:
            lit |= {
                row[0] for row in conn.execute(
                    "select distinct clause_id from event "
                    "where session_id = %s and clause_id is not null",
                    (session_id,),
                ).fetchall()
            }
    # 未点亮的给 null——前端画**暗态**，不是不可用的灰：
    # 它是「还没问到」，不是「不支持」。
    out["labels"] = [
        {"id": cid, "display": display(cid, annex=annex, heading=heading),
         "note": authority_note(rank)} if cid in lit else None
        for cid, rank, annex, heading in nodes
    ]
    return out
