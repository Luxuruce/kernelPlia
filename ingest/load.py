"""把 knowledge/ppwr/ 的 YAML 装进库。

内容正本是 `businessSYETEM/knowledge/ppwr/`（产品侧维护，开发侧只读）；
库里的数据是它的**派生物**，所以本脚本是全量重载：先清空四张表再灌，
不做增量合并——增量会让库和正本悄悄分叉，而正本才是可审计的那一份。

    ~/.venvs/kernel/bin/python -m ingest.load            # 校验 + 装载
    ~/.venvs/kernel/bin/python -m ingest.load --check    # 只校验不落盘
    ~/.venvs/kernel/bin/python -m ingest.load --verbose  # 附每个单元的明细

装载约定见 `knowledge/ppwr/README.md` 第 2 节，五条全部实现在这里与 corpus.py。
`status` 一律照 YAML 装载——现在全是 draft，**装完也查不出答案，这是设计如此**：
只有 status='adopted' 且具名的条款可被检索（规范 K2、红线第 2 条）。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import psycopg
import yaml
from psycopg.types.json import Jsonb

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import CORPUS_DIR, CORPUS_ROOT, KNOWLEDGE_DIR, settings  # noqa: E402
from ingest.corpus import (  # noqa: E402
    NO_BBOX_SOURCES,
    CorpusChanged,
    bbox_from_lines,
    load_extract,
    text_en_from_lines,
    verify_corpus,
)

# 条款批次按 glob 收，不写死文件名——A2 每录一批就多一个 clauses.*.yaml
#（批 1 是 clauses.obligations.yaml 与 clauses.epr.yaml），
# 写死会让产品侧新增一批就得改代码，而且漏装不报错、只是悄悄少答。
CLAUSE_GLOB = "clauses.*.yaml"
ASSERTION_FILE = "assertions.yaml"
LINK_FILE = "links.yaml"
CONFLICT_FILE = "conflicts.yaml"

# 装载约定四：YAML 内的信息性字段，不入库，装载时忽略即可。
# full_section_lines 记整节范围（说明某单元为何只取其中一段）；
# src.bbox_available 标记无坐标边车的来源。丢了不影响功能。
INFORMATIONAL_KEYS = {"full_section_lines"}
INFORMATIONAL_SRC_KEYS = {"bbox_available"}

# 产品契约文档 第二节 / README 契约补正第 4 项
ALLOWED_SCOPE = {
    "all_packaging", "food_contact_only", "transport_only", "not_product_scoped",
}
ALLOWED_LINK_TYPE = {
    "requires", "derogates", "explains", "amends", "depends_on_pending",
}


class Report:
    """校验结果。errors 非空即拒绝落盘。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def read_yaml(name: str) -> dict:
    """索引目录里的一份 YAML。**缺文件按空处理**——演示切片没有署名断言
    （那是具名专家判断，不随代码公开），少了它不该让装载失败。"""
    f = KNOWLEDGE_DIR / name
    return yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}


# ---------------------------------------------------------------------------
# YAML → 记录
# ---------------------------------------------------------------------------

def build_clause(entry: dict, defaults: dict, rep: Report) -> dict | None:
    """把一个 YAML 条款单元变成一行 clause。"""
    cid = entry.get("id")
    if not cid:
        rep.error(f"条款单元缺 id：{entry!r:.120}")
        return None

    source_id = entry.get("source_id") or defaults.get("source_id")
    if not source_id:
        rep.error(f"{cid}：缺 source_id，且 defaults 里也没有")
        return None

    src = dict(entry.get("src") or {})
    for k in INFORMATIONAL_SRC_KEYS:
        src.pop(k, None)
    line_from, line_to = src.get("line_from"), src.get("line_to")

    override = entry.get("text_en_override") or entry.get("text_en")
    text_en, bbox = None, None

    # 演示切片把 text_en 直接写在 YAML 里（欧盟公报原文本就公开），
    # 所以没有语料提取件也能装载；真实索引仍按行号从提取件取，那才是唯一正本。
    if override and not (CORPUS_DIR.exists() and line_from):
        # 契约补正第 5 项：提取件有缺陷时用人工原文。全批仅 PFAS 定义段一处。
        text_en = override.strip()
        if line_from and line_to and CORPUS_DIR.exists():
            # 没有坐标边车就没有高亮框——前端按契约降级为只给页码，不报错。
            bbox = bbox_from_lines(source_id, line_from, line_to)
    elif line_from and line_to:
        ext = load_extract(source_id)
        if line_to > ext.n_lines:
            rep.error(
                f"{cid}：行号区间 {line_from}–{line_to} 超出 {ext.key}.txt 的 {ext.n_lines} 行"
            )
            return None
        text_en = text_en_from_lines(
            source_id, line_from, line_to, entry.get("exclude_lines")
        )
        if not text_en:
            rep.error(f"{cid}：按行号 {line_from}–{line_to} 取到的 text_en 为空")
            return None
        bbox = bbox_from_lines(source_id, line_from, line_to)
        if not bbox and source_id not in NO_BBOX_SOURCES:
            rep.error(f"{cid}：{line_from}–{line_to} 一个坐标都没 join 到，边车可能不对版")

    is_inference = bool(entry.get("is_inference", defaults.get("is_inference", False)))
    if text_en is None and not is_inference:
        # 非署名断言必须有作准文本，等价于产品契约文档 DDL 的 text_en not null
        rep.error(f"{cid}：非署名断言却没有 text_en（既无行号也无 override）")
        return None

    scope = entry.get("scope")
    if scope and scope not in ALLOWED_SCOPE:
        rep.error(f"{cid}：scope 取值 {scope!r} 不在允许集内 {sorted(ALLOWED_SCOPE)}")

    path = entry.get("path") or {}
    return {
        "id": cid,
        "source_id": source_id,
        "path_article": path.get("article"),
        "path_paragraph": path.get("paragraph"),
        "path_point": path.get("point"),
        "annex": entry.get("annex"),
        "heading": entry.get("heading"),
        "text_en": text_en,
        "text_zh": entry.get("text_zh"),
        "lang_authenticity_zh": entry.get(
            "lang_authenticity_zh", defaults.get("lang_authenticity_zh")
        ),
        "page_from": src.get("page_from"),
        "page_to": src.get("page_to"),
        "bbox": Jsonb(bbox) if bbox else None,
        "line_from": line_from,
        "line_to": line_to,
        "scope": scope,
        "effective_expr": _jsonb(entry.get("effective_expr")),
        "numeric_limits": _jsonb(entry.get("numeric_limits")),
        "pending_dependency": _jsonb(entry.get("pending_dependency")),
        "notes": entry.get("notes"),
        "text_en_override": override,
        "assertion_body": None,
        "is_inference": is_inference,
        "adopted_by": entry.get("adopted_by", defaults.get("adopted_by")),
        "adopted_at": entry.get("adopted_at", defaults.get("adopted_at")),
        "status": entry.get("status", defaults.get("status", "draft")),
        "version": entry.get("version", defaults.get("version", 1)),
    }


def build_assertion(entry: dict, rep: Report) -> dict | None:
    """署名断言 → clause，is_inference = true。

    断言按定义就是「官方文件未覆盖」，**没有作准文本可锚**——YAML 里既无 src
    也无行号。四个正文字段装进 assertion_body（契约补正第 8 项）。
    """
    row = build_clause(entry, {"is_inference": True}, rep)
    if row is None:
        return None

    body = {
        k: entry[k]
        for k in ("question_zh", "coverage_check", "reasoning_zh", "output_requirements")
        if k in entry
    }
    if not body:
        rep.error(f"{row['id']}：署名断言没有任何 assertion_body 字段")
        return None
    if not row["is_inference"]:
        rep.error(f"{row['id']}：在 assertions.yaml 里却 is_inference 不为 true")

    row["assertion_body"] = Jsonb(body)
    return row


def _jsonb(v):
    if v is None or v == [] or v == {}:
        return None
    return Jsonb(v)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate(clauses, links, conflicts, rep: Report) -> None:
    ids = set()
    for c in clauses:
        if c["id"] in ids:
            rep.error(f"条款 id 重复：{c['id']}")
        ids.add(c["id"])

    for ln in links:
        for end in ("from_clause_id", "to_clause_id"):
            if ln[end] not in ids:
                rep.error(
                    f"链接指向不存在的条款：{ln['from_clause_id']} --{ln['link_type']}--> "
                    f"{ln['to_clause_id']}（{end} 缺行）"
                )
        if ln["link_type"] not in ALLOWED_LINK_TYPE:
            rep.error(f"link_type 取值不允许：{ln['link_type']}")

    for cf in conflicts:
        missing = [i for i in cf["clause_ids"] if i not in ids]
        if missing:
            rep.error(f"冲突 {cf['id']} 的 clause_ids 有缺行的条款：{missing}")
        trig = cf["trigger_clause_ids"]
        if not trig:
            rep.error(f"冲突 {cf['id']} 的 trigger_clause_ids 为空——无法判定阻断")
        extra = [i for i in trig if i not in cf["clause_ids"]]
        if extra:
            rep.error(f"冲突 {cf['id']}：trigger_clause_ids 不是 clause_ids 的子集，多出 {extra}")

    # 触发条款不得由链接扩展带入命中集（产品契约文档 第三节的排除规则）。
    # 这里只报信息，让实现 该任务 时知道哪些条款真的够得着、必须挂排除标记。
    #
    # 扩展方向按产品契约文档 第三节「一跳链接扩展」：
    #   explains                              单向，从被解释的条款拉回解释它的，
    #                                         即命中 to 端时带入 from 端；
    #   derogates / amends / depends_on_pending  双向，任一端命中都带入另一端；
    #   requires                              不自动扩展。
    trigger_all = {i for cf in conflicts for i in cf["trigger_clause_ids"]}
    reachable: dict[str, list[str]] = {}
    for ln in links:
        t, f, to = ln["link_type"], ln["from_clause_id"], ln["to_clause_id"]
        if t == "requires":
            continue
        pairs = [(f, to)] if t == "explains" else [(f, to), (to, f)]
        for brought_in, via in pairs:
            if brought_in in trigger_all:
                reachable.setdefault(brought_in, []).append(f"{via} --{t}-->")
    if reachable:
        rep.warn(
            "这些触发条款够得着链接扩展，该任务 必须把它们排除在扩展之外"
            "（漏了会造成大面积误阻断）："
        )
        for cid, vias in sorted(reachable.items()):
            rep.warn(f"    {cid}  ← 命中 {'、'.join(sorted(set(vias)))} 时会被带入")


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

CLAUSE_COLS = [
    "id", "source_id", "path_article", "path_paragraph", "path_point", "annex",
    "heading", "text_en", "text_zh", "lang_authenticity_zh", "page_from", "page_to",
    "bbox", "line_from", "line_to", "scope", "effective_expr", "numeric_limits",
    "pending_dependency", "notes", "text_en_override", "assertion_body",
    "is_inference", "adopted_by", "adopted_at", "status", "version",
]


def write(conn, clauses, links, conflicts) -> None:
    # 全量重载。顺序：先删依赖方，再删被依赖方；插入反过来。
    conn.execute("delete from conflict")
    conn.execute("delete from clause_link")
    conn.execute("delete from clause")

    cols = ", ".join(CLAUSE_COLS)
    ph = ", ".join(["%s"] * len(CLAUSE_COLS))
    conn.cursor().executemany(
        f"insert into clause ({cols}) values ({ph})",
        [tuple(c[k] for k in CLAUSE_COLS) for c in clauses],
    )
    conn.cursor().executemany(
        "insert into clause_link (from_clause_id, to_clause_id, link_type, note) "
        "values (%s, %s, %s, %s)",
        [
            (l["from_clause_id"], l["to_clause_id"], l["link_type"], l["note"])
            for l in links
        ],
    )
    conn.cursor().executemany(
        "insert into conflict (id, clause_ids, summary_zh, resolution_zh, "
        "authority_basis, trigger_clause_ids, trigger_context_terms, trigger_hint) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (
                c["id"], c["clause_ids"], c["summary_zh"], c["resolution_zh"],
                c["authority_basis"], c["trigger_clause_ids"],
                c["trigger_context_terms"], c["trigger_hint"],
            )
            for c in conflicts
        ],
    )


# ---------------------------------------------------------------------------

def collect(rep: Report):
    clauses = []
    batch_files = sorted(p.name for p in KNOWLEDGE_DIR.glob(CLAUSE_GLOB))
    if not batch_files:
        rep.error(f"{KNOWLEDGE_DIR} 下没有 {CLAUSE_GLOB}——条款批次一个都没找到")
    for name in batch_files:
        doc = read_yaml(name)
        defaults = doc.get("defaults") or {}
        for entry in doc.get("clauses") or []:
            row = build_clause(entry, defaults, rep)
            if row:
                row["_batch"] = name
                clauses.append(row)

    doc = read_yaml(ASSERTION_FILE)
    for entry in doc.get("assertions") or []:
        row = build_assertion(entry, rep)
        if row:
            clauses.append(row)

    doc = read_yaml(LINK_FILE)
    links = [
        {
            "from_clause_id": l["from"],
            "to_clause_id": l["to"],
            "link_type": l["type"],
            "note": l.get("note"),
        }
        for l in doc.get("links") or []
    ]

    doc = read_yaml(CONFLICT_FILE)
    conflicts = [
        {
            "id": c["id"],
            "clause_ids": c["clause_ids"],
            "summary_zh": c["summary_zh"],
            "resolution_zh": c["resolution_zh"],
            "authority_basis": c["authority_basis"],
            "trigger_clause_ids": c.get("trigger_clause_ids") or [],
            "trigger_context_terms": c.get("trigger_context_terms"),
            "trigger_hint": c.get("trigger_hint"),
            # trigger_reason 与 note 是给人看的推导记录，DDL 无对应列，不入库
        }
        for c in doc.get("conflicts") or []
    ]
    return clauses, links, conflicts


def summarize(clauses, links, conflicts, verbose: bool) -> None:
    by_source: dict[str, int] = {}
    for c in clauses:
        by_source[c["source_id"]] = by_source.get(c["source_id"], 0) + 1

    print(f"条款单元 {len(clauses)}：", end="")
    print("，".join(f"{k} {v}" for k, v in sorted(by_source.items())))

    by_batch: dict[str, int] = {}
    for c in clauses:
        b = c.get("_batch") or "assertions.yaml"
        by_batch[b] = by_batch.get(b, 0) + 1
    print("  批次：" + "，".join(f"{k} {v}" for k, v in sorted(by_batch.items())))

    n_bbox = sum(1 for c in clauses if c["bbox"] is not None)
    n_notes = sum(1 for c in clauses if c["notes"])
    n_pend = sum(1 for c in clauses if c["pending_dependency"] is not None)
    n_ovr = sum(1 for c in clauses if c["text_en_override"])
    n_inf = sum(1 for c in clauses if c["is_inference"])
    n_adopted = sum(1 for c in clauses if c["status"] == "adopted")

    print(f"  带 bbox {n_bbox} · 带 notes {n_notes} · pending_dependency {n_pend} "
          f"· text_en_override {n_ovr} · 署名断言 {n_inf}")
    print(f"链接 {len(links)} · 冲突 {len(conflicts)}")
    print(f"**status=adopted 的 {n_adopted} 条**"
          f"{'——未采纳，检索查不出东西，这是设计如此' if n_adopted == 0 else ''}")

    if verbose:
        print("\n每个单元：")
        for c in sorted(clauses, key=lambda x: x["id"]):
            nb = 0 if c["bbox"] is None else len(c["bbox"].obj)
            print(f"  {c['id']:<38} 行 {c['line_from']}–{c['line_to']:<6} "
                  f"框 {nb:<3} {'notes' if c['notes'] else '     '} {c['status']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把 knowledge/ppwr 的 YAML 装进库")
    ap.add_argument("--check", action="store_true", help="只校验不落盘")
    ap.add_argument("--verbose", action="store_true", help="附每个单元的明细")
    args = ap.parse_args()

    # 装载前先校验语料版本。不一致即中止——行号漂移不会自己暴露，
    # 只会让每一条引用悄悄指错地方（MANIFEST.md 第 2 节）。
    try:
        if not CORPUS_DIR.exists():
            # 没有语料提取件 = 走演示切片那条路（text_en 内联）。
            # **哈希校验只在没有语料时跳过，不是取消**——行号是条款单元的锚，
            # 语料一漂引用就会悄悄指错地方，那道闸门必须在。
            print(f"未找到语料提取件（{CORPUS_DIR}），按内联 text_en 装载")
            n_files = 0
        else:
            n_files = verify_corpus()
    except CorpusChanged as e:
        print(f"✗ 语料校验不通过，拒绝装载：\n{e}")
        return 1
    print(f"语料校验通过：{n_files} 个文件与 {settings.corpus_version} 基线一致")
    print(f"  {CORPUS_ROOT}\n")

    rep = Report()
    clauses, links, conflicts = collect(rep)
    validate(clauses, links, conflicts, rep)

    summarize(clauses, links, conflicts, args.verbose)

    if rep.warnings:
        print(f"\n提示 {len(rep.warnings)} 条：")
        for w in rep.warnings:
            print(f"  · {w}")

    if rep.errors:
        print(f"\n✗ 校验不通过，{len(rep.errors)} 项，拒绝落盘：")
        for e in rep.errors:
            print(f"  · {e}")
        return 1

    if args.check:
        print("\n✓ 校验通过（--check，未落盘）")
        return 0

    with psycopg.connect(settings.database_url) as conn:
        write(conn, clauses, links, conflicts)
        conn.commit()
        n = conn.execute("select count(*) from clause").fetchone()[0]
        nl = conn.execute("select count(*) from clause_link").fetchone()[0]
        nc = conn.execute("select count(*) from conflict").fetchone()[0]
    print(f"\n✓ 已装载：clause {n} · clause_link {nl} · conflict {nc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
