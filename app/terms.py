"""提问的解析：条款号精确定位 + 主题词抽取。

对应 V0_验收标准文档 该任务 判据的两半：「可按条款号精确定位」「可按主题词召回候选」。

**V0 不做向量检索**（规范决策 #3：主索引必须是结构化条文索引）。召回全靠主题词，
所以这里的抽取质量直接决定召回率——够不着的条款要靠 `retrieval.py` 的一跳链接扩展补。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import jieba
import yaml

from app.config import KNOWLEDGE_DIR

# ── 条款号精确定位 ──────────────────────────────────────────────────────
#
# 引用格式按 businessSYETEM/CLAUDE.md「引用格式」一节：
#   第 7(1)(a) 条 · 附件 V 第 2 点 · 指南第 19 节 · FAQ 第 X 章问 5

# 第 5(5)(c) 条 / 第 5(4) 条 / 第 70(3) 条。
# 量词收「条款项」三个：用户按引用格式写「第 5(5)(c) 项」的一样多。
RE_ART_PARA = re.compile(
    r"第\s*(\d+)\s*[（(]\s*(\d+)\s*[）)]\s*(?:[（(]\s*([a-z])\s*[）)]\s*)?[条款项]"
)
# 第 5 条（只到条，不带款）
RE_ART = re.compile(r"第\s*(\d+)\s*条")
# 附件 VII 第 2 点 / 附件 II
RE_ANNEX = re.compile(r"附件\s*([IVXLC]+)(?:\s*第\s*(\d+)\s*点)?")
# 指南第 5 节
RE_GUIDANCE = re.compile(r"指南\s*第\s*(\d+)\s*节")
# FAQ 第 III 章问 14
RE_FAQ = re.compile(r"FAQ\s*第\s*([IVXLC]+)\s*章\s*问\s*(\d+)", re.I)


@dataclass(frozen=True)
class ClauseRef:
    """一处条款号引用。字段为 None 表示「这一级不限定」。"""

    source_id: str
    article: str | None = None
    paragraph: str | None = None
    point: str | None = None
    annex: str | None = None
    guidance_section: str | None = None
    faq_chapter: str | None = None
    faq_question: str | None = None


def parse_clause_refs(question: str) -> list[ClauseRef]:
    """从提问里抽出条款号引用。抽不到返回空表——那时全靠主题词召回。"""
    refs: list[ClauseRef] = []
    seen: set[ClauseRef] = set()

    def add(ref: ClauseRef) -> None:
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for art, para, point in RE_ART_PARA.findall(question):
        add(ClauseRef("ppwr_reg", article=art, paragraph=para, point=point or None))

    # 只到条的引用：已被上面带款的模式吃掉的不再重复登记
    with_para = {a for a, _, _ in RE_ART_PARA.findall(question)}
    for art in RE_ART.findall(question):
        if art not in with_para:
            add(ClauseRef("ppwr_reg", article=art))

    for annex, point in RE_ANNEX.findall(question):
        add(ClauseRef("ppwr_reg", annex=f"{annex}/{point}" if point else annex))

    for sec in RE_GUIDANCE.findall(question):
        add(ClauseRef("ppwr_guidance", guidance_section=sec))

    for ch, q in RE_FAQ.findall(question):
        add(ClauseRef("ppwr_faq", faq_chapter=ch.upper(), faq_question=q))

    return refs


# ── 主题词抽取 ──────────────────────────────────────────────────────────

CJK = r"一-鿿"
RE_CJK_RUN = re.compile(f"[{CJK}]+")
# 拉丁词与数字，保留 PFAS、mg/kg、2026-08-12、5(4) 这类形态
RE_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9./-]*|\d+(?:[.,]\d+)?(?:\s*(?:mg/kg|ppb|ppm|%|吨|年))?")

# 中文词组的长度范围。下限 2 是因为单字几乎命中全库、没有区分度；
# 上限 6 足够覆盖「受关注物质」「可重复充装」这类术语。
NGRAM_MIN, NGRAM_MAX = 2, 6

# 虚词字表。**只用于判定「整个词都是虚词」**——`怎么`『什么』『多少』『我们』
# 这类疑问词与代词逐字都在表内，整词丢弃即可。
#
# 改过一次口径：字符 n-gram 时代这张表还兼做「术语不得以虚词起讫」的剪枝，
# 那是没有分词器时的权宜。分词之后按首尾**字**剪会误伤真词——
# 「可回收性」「可重复使用」都以 `可` 开头，一剪 某条用例 就整条召回归零。
# 现在只按**词**判定，首尾字不再参与。
BOUNDARY_STOP = set(
    "的了吗呢吧啊呀哦么着过被把将从向对于在与和或及其之所是不也都还就很更最"
    "要会能可有没无这那些个我你他她它们怎什多少里为样如何请需咋嘛哪"
)


def _cjk_ngrams(run: str) -> list[str]:
    """先分词，再在**词的序列**上滑窗，而不是在字上滑窗。

    字级滑窗会切出中文里根本不存在的词，而它们照样能 `ilike` 命中条款正文。
    实测踩过一次：一句问重金属的提问被切出 `包装重`——它在提问里跨「包装|重金属」，
    在另一条讲重复使用的条款标题里跨「包装|重复」，**两边都不是中文里的词**，
    字面却一致。自足分数 3.74×3.0(heading) = 11.21 越过 10.0 闸门，误触发冲突 C2，
    随后 conflict_related 的最高保留优先级把召回排第一的第 5(4) 条挤出了上下文。

    词级滑窗保留了「长词权重更高」这一条（`包装` < `重金属` < `重金属超标`），
    只是不再凭空造词。相邻才拼——中间隔了被丢弃的虚词就不拼，
    否则又会造出「超标怎样」这类跨越语义的伪词。
    """
    words = [w for w in jieba.cut(run) if w.strip()]
    stop = [all(ch in BOUNDARY_STOP for ch in w) for w in words]
    out = []
    for i in range(len(words)):
        if stop[i]:
            continue
        g = ""
        for j in range(i, len(words)):
            if stop[j]:
                break                       # 虚词处断开，不跨过「的」「了」去拼
            g += words[j]
            if len(g) > NGRAM_MAX:
                break
            if len(g) < NGRAM_MIN:
                continue
            out.append(g)
    return out


@lru_cache(maxsize=1)
def _stopwords() -> frozenset[str]:
    """召回停用词表。**正本是 `knowledge/ppwr/stopwords.yaml`，产品侧维护。**

    收录判据、改动纪律、明确不收的词都写在那份文件里，这里只负责剪。
    大小写敏感——表里 `Packaging` 与 `packaging` 是两条独立记录，按字面照办。

    **文件缺失直接报错，不静默降级。** 少了这张表召回照样跑得动，
    只是又会开始误阻断——和语料哈希不符一样，是那种「不报错只指错」的漂移。
    """
    doc = yaml.safe_load((KNOWLEDGE_DIR / "stopwords.yaml").read_text(encoding="utf-8"))
    return frozenset(
        w["term"] for key in ("stopwords_zh", "stopwords_en") for w in (doc.get(key) or [])
    )


def extract_terms(question: str) -> dict[str, float]:
    """提问 → {主题词: 词长权重}。

    中文先分词、再在词序列上滑窗（见 `_cjk_ngrams`）。**长词权重更高**，
    「受关注物质」比「物质」更能指向正确条款，靠 len ** 1.2 拉开差距。
    压制噪声的是三件事：idf（见 retrieval.topic_recall）让命中全库的词权重趋近于零，
    分词让不存在的词根本不进来，停用词表（见 `_stopwords`）剪掉语料自名词与样板语。
    """
    terms: dict[str, float] = {}

    stop = _stopwords()

    def add(t: str) -> None:
        t = t.strip().strip(".,;:/-")
        if len(t) < 2:
            return
        # 停用词按**整词**剪：`PPWR` 丢，`PPWR 第 67(5) 条` 不丢。
        # 与虚词剪枝同一条纪律——按片段剪会误伤领域词（见 _cjk_ngrams）。
        if t in stop:
            return
        terms[t] = max(terms.get(t, 0.0), len(t) ** 1.2)

    for run in RE_CJK_RUN.findall(question):
        for g in _cjk_ngrams(run):
            add(g)

    for tok in RE_LATIN_RUN.findall(question):
        add(tok)

    # 「100 mg/kg」「50 mg/kg」这类数值加单位是关键区分词，单独成词再加一遍
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(mg/kg|ppb|ppm|%)", question, re.I):
        add(f"{m.group(1)} {m.group(2)}")
        add(m.group(0))

    return terms
