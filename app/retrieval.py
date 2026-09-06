"""该任务 结构化检索：命中集怎么形成。

规格出自 产品契约文档 第三节「命中集怎么形成：一跳链接扩展」，一字不改地实现：

    命中集 = 直接命中（条款号精确定位 ∪ 主题词召回）  ∪  一跳链接扩展

**主索引是结构化条文索引，不是向量索引**（规范决策 #3、原则二）：适用性、优先级、
生效时点是规则问题，检索只负责把人带到正确的条文面前，判断本身走显式规则。

两条贯穿全模块的闸门，任何一条 SQL 都不得绕过：
  · 只有 `clause.status = 'adopted'` 的条款可被检索（红线第 2 条）
  · 只有 `source.rights_status = 'ALLOW'` 的来源可被检索（规范 6.4 权利闸、红线第 12 条）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.terms import ClauseRef, extract_terms, parse_clause_refs

Origin = Literal["exact", "topic", "expanded", "conflict_related"]

# 产品契约文档 第三节「一跳扩展分三档」
MUST_EXPAND = ("derogates", "amends", "depends_on_pending")   # 无条件必扩，双向
LIMITED_EXPAND = "explains"                                   # 限额扩，单向
# `requires` 不自动扩展——目标条款自身的主题词命中率足够

# 每条被命中条款最多带回几条解释它的条款。
# 不限额则最多多带 9 条、约 1 550 token，占 10 000 闸门 15%（产品契约文档 第三节实测）。
EXPLAINS_LIMIT_PER_CLAUSE = 3

TOPIC_RECALL_LIMIT = 12

# 该项改动：`requires` 限额扩，**双向、单跳、不递归**，每条被命中条款最多 2 条。
# 2026-09-06 产品拍板批准，已上线（产品契约文档 第三节三档表）。
EXPAND_REQUIRES = True
REQUIRES_LIMIT_PER_CLAUSE = 2

# ── 该项改动 + 该项改动：机制已实现，**等产品侧确认后才上线**────────────────────────
#
# 产研边界约定第 7 节：该项改动「门禁中，须与 该项改动 同批上线」；该项改动「等开发提机制，
# 产品侧只给验收判据」，机制须「写回《待决项清单》，产品侧确认后才上线」。
# 所以这里是一个开关管两件事——它们本来就必须同批，分开上线会重现 该项改动 要修的形态。
#
# 打开后的三处变化：
#   一、冲突触发改用**自足分数闸门**（见 TRIGGER_SCORE_THRESHOLD），不再看命中集；
#   二、排除规则删除——触发条款可以由链接扩展带入；
#   三、`explains` 反向上扩启用（该项改动），不占限额。
# 2026-09-06 产品拍板 该项裁决：两分法、收窄排除规则、伴随呈现全部接受，**已上线**。
# 阈值「有条件接受」的那个条件（加固一条触发条款的 heading）已落地并实测生效，
# 见下方 TRIGGER_SCORE_THRESHOLD。
TRIGGER_DECOUPLED = True

# 该项改动 定标值。自足分数 ≥ 此值即触发。
#
# 定标方法按产品契约文档 第三节：以 15 条黄金用例为基线，取「全部 expect_conflict_ids
# 非空的用例都触发、且误阻断不超过当前的 1」的最宽阈值。
#
# 实测分数阶梯（词长权重 len**1.2 × 字段权重）：
#     2 字词在正文     4.6      2 字泛词在标题   6.9   ← 「包装」这类，不该触发
#     4 字专名在正文  10.6      3 字词在标题    11.2   ← 「PPWR」这类，该触发
# 需要触发的下界是 某条用例 的 某条冲突 = 10.6；不该触发的上界是 C2 在 某条用例/某条用例/某条用例/某条用例/某条用例
# 上的 6.9（全都来自 heading 里的「包装」二字）。取 10.0，卡在泛词与专名之间。
#
# **本阈值以「加固某条触发条款的 heading」为前提**——产品侧指出
# 10.0 距下界只有 0.6、距上界 3.1，**偏向漏阻断，与「宁可误阻断」相反**；
# 该项裁决 把 某条冲突 从 10.56 抬到 15.8 之后两侧余量变 3.1 / 5.8，阈值才站得住。
# **两者是同一问题的两面，不是各自独立的改动。**
#
# 正本在 `knowledge/ppwr/conflicts.yaml` 的 `meta.trigger_threshold`；
# 改它须按产品契约文档第三节的定标方法重做，并回归全部 15 条黄金用例。
TRIGGER_SCORE_THRESHOLD = 10.0


@dataclass
class Hit:
    clause_id: str
    origin: Origin
    score: float = 0.0
    # 扩展进来的条款照常进 citations，但要与直接命中可区分——
    # 否则「主动带出了减损」与「答非所问」在数据上分不开（产品契约文档 第三节末）
    via: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.origin in ("exact", "topic")


@dataclass
class Retrieval:
    hits: list[Hit] = field(default_factory=list)
    # 闸门命中的冲突 → 阻断，next_action = ack_conflict
    conflict_ids: list[str] = field(default_factory=list)
    # 触发条款进了引用但闸门没拦 → **随答案强制呈现，不阻断**（见 accompanying_conflicts）
    accompanying_conflict_ids: list[str] = field(default_factory=list)
    terms: dict[str, float] = field(default_factory=dict)
    refs: list[ClauseRef] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [h.clause_id for h in self.hits]

    @property
    def direct_ids(self) -> set[str]:
        return {h.clause_id for h in self.hits if h.is_direct}

    def by_id(self, clause_id: str) -> Hit | None:
        return next((h for h in self.hits if h.clause_id == clause_id), None)


# 可被检索的条款。两条闸门都在这里，其余查询一律 join 它。
RETRIEVABLE = """
    select c.*, s.authority_rank
    from clause c join source s on s.id = c.source_id
    where c.status = 'adopted' and s.rights_status = 'ALLOW'
"""


# ── 一、条款号精确定位 ──────────────────────────────────────────────────

# 只给条号（如「第 5 条」）时，该条下最多取几个单元。
#
# 起初是「全取，宁宽勿窄」——实测击穿成本闸门：某条用例「第 5 条的合规怎么证明？」
# 一次命中第 5 条全部 17 个单元，加扩展共 44 条、约 21 600 token，
# 是 10 000 闸门的 216%。产品契约文档 第六节对这种情形的规定就是
# 「超出则**先缩小召回范围**再调用」，所以按主题词相关度取前几个，而不是全要。
ARTICLE_LEVEL_CAP = 6


def locate_by_refs(
    conn, refs: list[ClauseRef], terms: dict[str, float] | None = None
) -> list[str]:
    """按条款号精确定位。

    给到款项（「第 5(4) 条」）时精确取那一个；**只给到条时不再全取**，
    按主题词相关度取前 ARTICLE_LEVEL_CAP 个——理由见该常量注释。
    """
    terms = terms or {}
    if not refs:
        return []

    found: list[str] = []
    for r in refs:
        conds = ["c.source_id = %(source_id)s"]
        params: dict = {"source_id": r.source_id}

        if r.annex:
            # 「附件 VII」应命中 VII 下全部条目，「附件 VII 第 2 点」只命中 VII/2
            if "/" in r.annex:
                conds.append("c.annex = %(annex)s")
                params["annex"] = r.annex
            else:
                conds.append("(c.annex = %(annex)s or c.annex like %(annex_pref)s)")
                params["annex"] = r.annex
                params["annex_pref"] = f"{r.annex}/%"
        elif r.guidance_section:
            # 指南第 5 节被切成 sec/5、sec/5/steps、sec/5/market 三段，三段都要
            conds.append("(c.id = %(gid)s or c.id like %(gid_pref)s)")
            params["gid"] = f"ppwr_guidance/sec/{r.guidance_section}"
            params["gid_pref"] = f"ppwr_guidance/sec/{r.guidance_section}/%"
        elif r.faq_chapter:
            conds.append("c.id = %(fid)s")
            params["fid"] = f"ppwr_faq/ch/{r.faq_chapter}/q/{r.faq_question}"
        else:
            conds.append("c.path_article = %(article)s")
            params["article"] = r.article
            if r.paragraph:
                conds.append("c.path_paragraph = %(paragraph)s")
                params["paragraph"] = r.paragraph
            if r.point:
                conds.append("c.path_point = %(point)s")
                params["point"] = r.point

        rows = conn.execute(
            f"select c.id from ({RETRIEVABLE}) c where {' and '.join(conds)} order by c.id",
            params,
        ).fetchall()
        ids = [r0[0] for r0 in rows]

        # 只给到条、且该条单元多于上限时，按主题词相关度收窄。
        # 「第 5 条的合规怎么证明」应该拿到第 5(6) 条，而不是把 17 款全端上来。
        article_only = r.article and not r.paragraph and not r.annex
        if article_only and len(ids) > ARTICLE_LEVEL_CAP:
            sc = self_scores(conn, terms, ids)
            ids.sort(key=lambda cid: (-sc.get(cid, 0.0), cid))
            ids = ids[:ARTICLE_LEVEL_CAP]

        found.extend(ids)

    return list(dict.fromkeys(found))


# ── 二、主题词召回 ──────────────────────────────────────────────────────

# **只索引 heading + text_en + text_zh，不索引 notes**（产品契约文档 第三节、复核 V6）。
# notes 是答题纪律不是条文内容，拿它召回会让「不点名实验室」这类运营口径变成检索入口。
TOPIC_SQL = f"""
with base as ({RETRIEVABLE}),
     q(term, tw) as (select * from unnest(%(terms)s::text[], %(weights)s::float8[])),
     m as (
       select b.id,
              q.term,
              q.tw,
              case
                when b.heading is not null and b.heading ilike '%%' || q.term || '%%' then 3.0
                when b.text_zh is not null and b.text_zh ilike '%%' || q.term || '%%' then 2.0
                when b.text_en is not null and b.text_en ilike '%%' || q.term || '%%' then 1.5
              end as fw
       from base b cross join q
     ),
     hit as (select * from m where fw is not null),
     tot as (select count(*)::float8 n from base),
     df  as (select term, count(distinct id)::float8 n from hit group by term),
     -- 命中大半个库的词没有区分度：「包装」命中 45/63，靠 idf 只压到 0.87，
     -- 再乘 heading 权重仍足以把无关条款顶进命中集。直接丢掉比调权重可靠。
     kept as (select term, n from df where n <= (select n from tot) * %(df_max_ratio)s)
select h.id,
       sum(h.tw * h.fw * ln(1 + (select n from tot) / d.n)) as score
from hit h join kept d on d.term = h.term
group by h.id
order by score desc, h.id
limit %(limit)s
"""

# 命中超过全库这个比例的词直接丢弃
DF_MAX_RATIO = 0.4
# 只保留分数达到最高分这个比例的候选。绝对阈值随提问长度漂移，相对阈值不会。
SCORE_MIN_RATIO = 0.15


def topic_recall(conn, terms: dict[str, float], limit: int = TOPIC_RECALL_LIMIT):
    """主题词召回。

    评分 = Σ 词长权重 × 字段权重 × idf。

    **idf 是这里的关键**：中文按 n-gram 切会产出大量「包装」「物质」这类命中全库的词，
    靠 idf 把它们压到接近零，让「受关注物质」「托盘缠绕膜」这类长而稀有的词决定排序。
    字段权重取最高命中的一项而非累加——同一个词在标题和正文都出现不代表更相关。
    """
    if not terms:
        return []
    # ILIKE 的通配符要转义，否则提问里出现 % 或 _ 会变成通配
    esc = {t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"): w
           for t, w in terms.items()}
    rows = conn.execute(
        TOPIC_SQL,
        {
            "terms": list(esc.keys()),
            "weights": list(esc.values()),
            "limit": limit,
            "df_max_ratio": DF_MAX_RATIO,
        },
    ).fetchall()
    if not rows:
        return []
    top = float(rows[0][1])
    return [
        (r[0], float(r[1])) for r in rows if float(r[1]) >= top * SCORE_MIN_RATIO
    ]


# ── 三、一跳链接扩展 ────────────────────────────────────────────────────

def trigger_clause_ids(conn) -> set[str]:
    """全部冲突的触发条款。

    产品契约文档 第三节的排除规则：**属于任一冲突 trigger_clause_ids 的条款，
    不得由链接扩展带入命中集——只能直接命中才进。**

    漏了这条的实测后果：一条解释性单元 `--explains-->` 指向某条法条，命中那条法条就会
    把触发条款一起带进来，于是**每一个沾边的提问都被阻断**；
    `q/21 --explains--> art/5/4` 会让 某条用例、某条用例、某条用例 三条 A1 用例全部被误阻断。
    """
    rows = conn.execute(
        "select distinct unnest(trigger_clause_ids) from conflict"
    ).fetchall()
    return {r[0] for r in rows}


def expand_one_hop(conn, direct: list[Hit], topic_scores: dict[str, float]) -> list[Hit]:
    """一跳扩展。三档规则见 MUST_EXPAND / LIMITED_EXPAND / requires。"""
    if not direct:
        return []

    direct_ids = [h.clause_id for h in direct]
    seen = set(direct_ids)
    out: list[Hit] = []

    # 排除规则：触发条款不得由扩展带入。
    #
    # **该项改动 落地后这条规则收窄，只保留向下 explains 一处。** 原规则是「触发只看直接命中集」
    # 这个前提下守不变量的权宜；触发判定摘出去之后前提消失，全条保留反而会挡住 该项改动 的
    # 反向上扩（C2 的触发条款是位阶 1 的第 29(1)(2)(3) 条），把答案打回
    # 「只有指南、没有法条」——正是 该项改动 要修的形态。
    #
    # 保留的那一半仍有道理，且方向是**不对称**的：
    #   · 向下 explains 拉的是「解释某条法条的指南/FAQ」。把一条「单独引用会误导」的条款
    #     当作别条的注脚顺带塞进来，风险高而相关性最弱——**继续排除**。
    #   · 向上 explains 拉的是「这条指南/FAQ 所解释的法条」，那是红线第 10 条的正向落实——**不排除**。
    #   · 必扩三类（derogates / amends / depends_on_pending）语义就是「不带就答错」——**不排除**。
    # 实测：整条删除与只保留向下，召回缺口与误阻断完全相同（2 / 1），
    # 但只保留向下能把「伴随呈现」的用例数从 6 条降到 4 条，噪声更低。
    excluded_downward = trigger_clause_ids(conn)
    excluded = set() if TRIGGER_DECOUPLED else excluded_downward

    # 必扩：derogates / amends / depends_on_pending，双向，无条件。
    # 语义就是「不带就答错」——减损是规则库一等公民（决策 #13），
    # amends 是「问 9 修正问 8」，depends_on_pending 承载「在此之前按什么执行」。
    rows = conn.execute(
        f"""
        select distinct other, l.link_type, anchor from (
          select l.to_clause_id   as other, l.from_clause_id as anchor, l.link_type
          from clause_link l where l.from_clause_id = any(%(ids)s)
          union all
          select l.from_clause_id as other, l.to_clause_id   as anchor, l.link_type
          from clause_link l where l.to_clause_id = any(%(ids)s)
        ) l
        join ({RETRIEVABLE}) c on c.id = l.other
        where l.link_type = any(%(types)s)
        order by other
        """,
        {"ids": direct_ids, "types": list(MUST_EXPAND)},
    ).fetchall()
    for other, link_type, anchor in rows:
        if other in seen or other in excluded:
            continue
        seen.add(other)
        out.append(Hit(other, "expanded", via=f"{anchor} --{link_type}-->"))

    # 限额扩：explains，单向——从直接命中的条款拉回**解释它的**那些。
    # 链接方向是 explainer --explains--> explained，所以命中 to 端时带入 from 端。
    # 每条被命中条款最多 3 条，按召回相关度取。
    for anchor in direct_ids:
        rows = conn.execute(
            f"""
            select l.from_clause_id
            from clause_link l
            join ({RETRIEVABLE}) c on c.id = l.from_clause_id
            where l.to_clause_id = %(anchor)s and l.link_type = %(t)s
            order by l.from_clause_id
            """,
            {"anchor": anchor, "t": LIMITED_EXPAND},
        ).fetchall()
        # 向下 explains：触发条款始终排除，与 TRIGGER_DECOUPLED 无关（见上方注释）
        cands = [
            r[0] for r in rows
            if r[0] not in seen and r[0] not in excluded and r[0] not in excluded_downward
        ]
        cands.sort(key=lambda cid: (-topic_scores.get(cid, 0.0), cid))
        for cid in cands[:EXPLAINS_LIMIT_PER_CLAUSE]:
            seen.add(cid)
            out.append(Hit(cid, "expanded", via=f"{anchor} --explains-->"))

    # 该项改动：explains 反向上扩，命中解释方时带入被解释的法条，**不占限额**。
    # 方向是位阶向上，不污染位阶——反而保证「引了解释就必须同时引被解释的法条」。
    # 与 该项改动 同批，见 TRIGGER_DECOUPLED。
    if TRIGGER_DECOUPLED:
        rows = conn.execute(
            f"""
            select distinct l.to_clause_id, l.from_clause_id
            from clause_link l
            join ({RETRIEVABLE}) c on c.id = l.to_clause_id
            where l.from_clause_id = any(%(ids)s) and l.link_type = 'explains'
            order by 1
            """,
            {"ids": direct_ids},
        ).fetchall()
        for other, anchor in rows:
            if other in seen or other in excluded:
                continue
            seen.add(other)
            out.append(Hit(other, "expanded", via=f"{anchor} --explains(上扩)-->"))

    # 该项改动（默认关闭）：requires 限额扩，双向。
    if EXPAND_REQUIRES:
        for anchor in direct_ids:
            rows = conn.execute(
                f"""
                select distinct other from (
                  select l.to_clause_id as other from clause_link l
                  where l.from_clause_id = %(a)s and l.link_type = 'requires'
                  union all
                  select l.from_clause_id as other from clause_link l
                  where l.to_clause_id = %(a)s and l.link_type = 'requires'
                ) t
                join ({RETRIEVABLE}) c on c.id = t.other
                order by other
                """,
                {"a": anchor},
            ).fetchall()
            cands = [r[0] for r in rows if r[0] not in seen and r[0] not in excluded]
            cands.sort(key=lambda cid: (-topic_scores.get(cid, 0.0), cid))
            for cid in cands[:REQUIRES_LIMIT_PER_CLAUSE]:
                seen.add(cid)
                out.append(Hit(cid, "expanded", via=f"{anchor} --requires-->"))

    return out


# ── 四、冲突触发 ────────────────────────────────────────────────────────

# **自足分数**：只由「提问的词 × 该条款自身的可索引文本」决定，别的条款一个字都不参与。
#
# 与主题词召回的三点区别，每一点都是为了满足 该项改动 的验收判据
# 「改动任意一条非触发条款的 heading 或 text，不得改变任何冲突的触发结果」：
#
#   一、**不含 idf**——idf 是全库量，别的条款一改 df 就变，正是要断开的那根耦合。
#       产品侧排除 df 阈值方案时讲的也是这个理由（语料扩到电池法会改变 PPWR 的阻断行为）。
#   二、**不占 top-N 名额、不受相对分数截断**——那两处让触发结果依赖别的条款得了多少分。
#   三、**取最强的单个词命中（max），不是求和**——实测求和会让 某条用例 的 C2 得 11.5 分而误触发，
#       误阻断从 1 升到 2；max 只认「有没有一个足够特定的词命中」，与阈值的语义也更贴。
TRIGGER_SCORE_SQL = f"""
select c.id,
       coalesce(max(case
         when c.heading is not null and c.heading ilike '%%' || t.term || '%%' then t.w * 3.0
         when c.text_zh is not null and c.text_zh ilike '%%' || t.term || '%%' then t.w * 2.0
         when c.text_en is not null and c.text_en ilike '%%' || t.term || '%%' then t.w * 1.5
       end), 0) as score
from ({RETRIEVABLE}) c,
     unnest(%(terms)s::text[], %(weights)s::float8[]) as t(term, w)
where c.id = any(%(ids)s::text[])
group by c.id
"""


def self_scores(conn, terms: dict[str, float], clause_ids: list[str]) -> dict[str, float]:
    """算一批条款各自的自足分数。

    两处用它：该项改动 的阻断闸门（见 detect_conflicts_decoupled），
    以及只给条号时的条内收窄（见 ARTICLE_LEVEL_CAP）——后者也需要一个
    对任意条款都算得出的分数，而主题词召回只覆盖前 N 条。
    """
    if not terms or not clause_ids:
        return {}
    esc = {t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"): w
           for t, w in terms.items()}
    rows = conn.execute(
        TRIGGER_SCORE_SQL,
        {"terms": list(esc.keys()), "weights": list(esc.values()), "ids": clause_ids},
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def detect_conflicts_decoupled(conn, question: str, terms: dict[str, float]) -> list[str]:
    """该项改动：冲突触发与召回解耦。

        阻断 ⟺ max(自足分数 over trigger_clause_ids) ≥ 阈值
               ∧（trigger_context_terms 为空 ∨ 提问命中其中任一词）

    **完全不看命中集**，因此改任何非触发条款的 heading 或 text 都不会改变触发结果——
    这就是 该项改动 的验收判据。触发条款自身改了则会变，那是应该的。
    """
    rows = conn.execute(
        "select id, trigger_clause_ids, trigger_context_terms from conflict order by id"
    ).fetchall()
    all_triggers = sorted({t for _, trig, _ in rows for t in trig})
    scores = self_scores(conn, terms, all_triggers)

    out = []
    for cid, trig, ctx_terms in rows:
        if ctx_terms and not any(t in question for t in ctx_terms):
            continue
        if max((scores.get(t, 0.0) for t in trig), default=0.0) >= TRIGGER_SCORE_THRESHOLD:
            out.append(cid)
    return out


def accompanying_conflicts(conn, hit_ids: set[str], gated: list[str]) -> list[str]:
    """守不变量：**若某条款会被引用、且单独引用它会误导，则必须同时呈现该冲突。**

    闸门只管「要不要拦一道」。但触发条款可能由链接扩展进入命中集而没被闸门拦下
    （该项改动 的反向上扩正会如此：C2 的触发条款第 29(1)(2)(3) 条是位阶 1，
    命中指南第 18/19/20 节时会被扩进来）。这时若不呈现冲突，答案里就会出现
    一条「单独引用会误导」的条款而无任何提示——红线第 10 条要防的形态。

    返回的冲突**随答案强制呈现、但不阻断**：闸门保持自足（该项改动 判据），
    呈现跟着引用走（不变量），两个关注点各管各的。
    """
    rows = conn.execute(
        """select id, trigger_clause_ids from conflict
           where trigger_clause_ids && %(ids)s::text[] order by id""",
        {"ids": list(hit_ids)},
    ).fetchall()
    return [cid for cid, _ in rows if cid not in gated]


def detect_conflicts(conn, direct_ids: set[str], question: str) -> list[str]:
    """判定阻断。

        阻断 ⟺ trigger_clause_ids ∩ 直接命中集 ≠ ∅
               ∧（trigger_context_terms 为空 ∨ 用户提问命中其中任一词）

    **只看直接命中集，不看扩展集**（产品契约文档 第三节排除规则）。
    **不得用 clause_ids 判定**——某条冲突 的 clause_ids 含第 5(4) 条，按它判 14 条用例误判 4 条。
    **trigger_hint 是给人看的自由文本，不参与判定。**
    """
    if not direct_ids:
        return []
    rows = conn.execute(
        """
        select id, trigger_context_terms
        from conflict
        where trigger_clause_ids && %(ids)s
        order by id
        """,
        {"ids": list(direct_ids)},
    ).fetchall()

    out = []
    for cid, ctx_terms in rows:
        # 留空表示条款命中即触发。只有 某条冲突 配了语境词——
        # 它的触发条款 FAQ 问 21 的 text_en 含 heavy metals 与 100 mg/kg（作准文本删不得），
        # 不加这道条件，「某材质的某项限值超标了吗」会被拦在气瓶冲突页上。
        if ctx_terms and not any(t in question for t in ctx_terms):
            continue
        out.append(cid)
    return out


def add_conflict_related(conn, hits: list[Hit], conflict_ids: list[str]) -> list[Hit]:
    """命中冲突后，把该冲突涉及但尚未进入命中集的条款一并带入。

    **不参与触发判定**，只保证冲突能被完整、分级地呈现。

    没有这条，某条冲突 的第 2(2) 条两条路都进不来（指向它的链接只有 q/21 --explains--> 2/2，
    方向与单向扩展相反；heading 与 text_zh 里也没有气瓶字样），
    于是答案里只剩第 5(4) 条与位阶 4 的 FAQ 问 21——正是红线第 10 条要防的形态。
    """
    if not conflict_ids:
        return []
    seen = {h.clause_id for h in hits}
    rows = conn.execute(
        f"""
        select distinct c.id, k.id as conflict_id
        from conflict k
        cross join lateral unnest(k.clause_ids) as m(cid)
        join ({RETRIEVABLE}) c on c.id = m.cid
        where k.id = any(%(cids)s)
        order by c.id
        """,
        {"cids": conflict_ids},
    ).fetchall()
    out = []
    for cid, conflict_id in rows:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(Hit(cid, "conflict_related", via=conflict_id))
    return out


# ── 管线 ────────────────────────────────────────────────────────────────

def retrieve(conn, question: str, topic_limit: int = TOPIC_RECALL_LIMIT) -> Retrieval:
    """完整检索管线。返回命中集与第一轮判定出的冲突。

    答案合成（该任务）在此之上，本函数不调模型。冲突判定放在检索阶段，
    所以阻断轮**不做答案合成也不调模型**，成本接近零（产品契约文档 第三节两轮交互）。
    """
    refs = parse_clause_refs(question)
    terms = extract_terms(question)

    topic = topic_recall(conn, terms, topic_limit)
    topic_scores = dict(topic)
    # 主题词分数先算，条号定位在只给到条时要用它收窄（见 ARTICLE_LEVEL_CAP）
    exact_ids = locate_by_refs(conn, refs, terms)

    hits: list[Hit] = []
    seen: set[str] = set()
    for cid in exact_ids:
        seen.add(cid)
        hits.append(Hit(cid, "exact", score=topic_scores.get(cid, 0.0)))
    for cid, score in topic:
        if cid not in seen:
            seen.add(cid)
            hits.append(Hit(cid, "topic", score=score))

    direct = list(hits)
    hits.extend(expand_one_hop(conn, direct, topic_scores))

    accompanying: list[str] = []
    if TRIGGER_DECOUPLED:
        # 闸门自足，不看命中集（该项改动 判据）
        conflict_ids = detect_conflicts_decoupled(conn, question, terms)
        # 呈现跟着引用走，守不变量
        accompanying = accompanying_conflicts(
            conn, {h.clause_id for h in hits}, conflict_ids
        )
    else:
        conflict_ids = detect_conflicts(conn, {h.clause_id for h in direct}, question)

    hits.extend(add_conflict_related(conn, hits, conflict_ids + accompanying))

    return Retrieval(
        hits=hits,
        conflict_ids=conflict_ids,
        accompanying_conflict_ids=accompanying,
        terms=terms,
        refs=refs,
    )
