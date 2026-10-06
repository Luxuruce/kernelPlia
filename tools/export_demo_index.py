"""从私有内容正本导出**演示索引**。

这个仓库是「合规内核」的**代码**。真正的资产是另一侧的条款索引——
逐条具名采纳的条款单元、链接、冲突与黄金用例。这里只放够跑起来的一小片。

**白名单导出，不是黑名单剥离。** 黑名单（「记得删掉 notes」）是 fail-open：
以后内容侧新增一个敏感字段，默认就跟着泄露。白名单是 fail-safe：
不在下面这几行里的字段，一个都出不去。这份文件本身就是那条边界的说明。

    python tools/export_demo_index.py <私有 knowledge 目录> [-o demo_index]

在私有仓里跑；公开仓保留它，是为了让边界可被复核，不是为了让你能跑它。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

# ── 白名单 ──────────────────────────────────────────────────────────────
CLAUSE_FIELDS = ("id", "source_id", "annex", "heading", "scope", "effective_expr",
                 "numeric_limits", "pending_dependency", "text_en", "src")
LINK_FIELDS = ("from", "to", "type")
CONFLICT_FIELDS = ("id", "clause_ids", "summary_zh", "resolution_zh", "authority_basis",
                   "trigger_clause_ids", "trigger_context_terms")
CASE_FIELDS = ("id", "type", "q", "expect_clauses", "expect_conflict_ids",
               "expect_next_action")
STOP_FIELDS = ("term", "why")
NEVER_STOP_FIELDS = ("term", "reason")

#: 明确**不出去**的字段，写在这里只为可读——真正拦住它们的是上面的白名单。
NEVER = ("notes", "text_zh", "adopted_by", "forbid", "expect_behavior", "is_inference")

# ── 演示切片 ────────────────────────────────────────────────────────────
# 选这十条不是随机：它们同时覆盖 `derogates` 必扩、`explains` 上扩、
# 以及一条真实的官方文件互相打架（授权决定豁免了、实施指南没反映）。
DEMO_CLAUSES = [
    "ppwr/art/5/4", "ppwr/art/5/5", "ppwr/art/70/3", "ppwr_faq/ch/III/q/17",
    "ppwr/art/29/1", "ppwr/art/29/2", "ppwr/art/29/3", "ppwr/art/29/4",
    "dec_2026_429/art/1", "ppwr_guidance/sec/19",
]
DEMO_CONFLICT = "C2_art29_pallet_wrap_exemption"
DEMO_CASES = ["G01", "G02", "G13"]


def pick(d: dict, fields: tuple[str, ...]) -> dict:
    return {k: d[k] for k in fields if d.get(k) is not None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("knowledge", type=pathlib.Path)
    ap.add_argument("-o", "--out", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parent.parent / "demo_index")
    a = ap.parse_args()
    K, OUT = a.knowledge, a.out
    OUT.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from ingest.corpus import text_en_from_lines          # 英文正文由行号从提取件取

    want = set(DEMO_CLAUSES)
    clauses = []
    for f in sorted(K.glob("clauses*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        defaults = doc.get("defaults") or {}
        for c in doc.get("clauses") or []:
            if c.get("id") not in want:
                continue
            out = pick(c, CLAUSE_FIELDS)
            sid = out.get("source_id") or defaults.get("source_id")
            out["source_id"] = sid
            src = c.get("src") or {}
            if src.get("line_from"):
                # text_en 是欧盟公报作准原文，本身就公开；该项裁决 的白名单里也点名了它。
                out["text_en"] = text_en_from_lines(
                    sid, src["line_from"], src["line_to"], c.get("exclude_lines"))
            clauses.append(out)
    got = {c["id"] for c in clauses}
    missing = want - got
    if missing:
        sys.exit(f"演示切片里这几条没找到：{sorted(missing)}")

    links = [pick(l, LINK_FIELDS)
             for l in yaml.safe_load((K / "links.yaml").read_text(encoding="utf-8"))["links"]
             if l["from"] in got and l["to"] in got]
    confs = []
    for c in yaml.safe_load((K / "conflicts.yaml").read_text(encoding="utf-8"))["conflicts"]:
        if c["id"] != DEMO_CONFLICT:
            continue
        out = pick(c, CONFLICT_FIELDS)
        for k in ("clause_ids", "trigger_clause_ids"):
            out[k] = [x for x in (out.get(k) or []) if x in got]
        confs.append(out)
    # 用例的期望引用**裁到演示切片之内**——不然一条用例的 expect_clauses
    # 就会把切片外的条款 id 报出去，等于用测试文件绕开白名单。
    cases = []
    for c in yaml.safe_load((K / "golden_cases.yaml").read_text(encoding="utf-8"))["cases"]:
        if c["id"] not in DEMO_CASES:
            continue
        out = pick(c, CASE_FIELDS)
        out["expect_clauses"] = [x for x in (out.get("expect_clauses") or []) if x in got]
        if out["expect_clauses"] or out.get("expect_conflict_ids"):
            cases.append(out)

    # 逐条复核：白名单之外的字段一个都不许在产物里出现
    def audit(rows: list[dict], allowed: tuple[str, ...]) -> None:
        for r in rows:
            bad = set(r) - set(allowed)
            assert not bad, f"白名单被绕过：{bad}"
            assert not (set(r) & set(NEVER)), f"命中禁列字段：{set(r) & set(NEVER)}"

    audit(clauses, CLAUSE_FIELDS); audit(links, LINK_FIELDS)
    audit(confs, CONFLICT_FIELDS); audit(cases, CASE_FIELDS)

    def dump(name: str, obj) -> None:
        (OUT / name).write_text(
            yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8")

    # status / adopted_by 由导出器统一标注：**演示切片不冒充具名采纳**。
    # 库里有 clause_adopted_requires_signature_ck——采纳必须有署名与时间，
    # 这条约束在演示切片上照样成立，所以这里如实写「非真实采纳记录」。
    # 日期取首次导出那天：条款没变就沿用上一版的日期，不然每发布一次公开仓
    # 就平白多出十处改动，真正的内容变化反而被淹掉。
    import datetime as _dt
    prev_f = OUT / "clauses.demo.yaml"
    prev = {}
    if prev_f.exists():
        for p in (yaml.safe_load(prev_f.read_text(encoding="utf-8")) or {}).get("clauses") or []:
            prev[p["id"]] = p
    today = _dt.date.today().isoformat()
    for c in clauses:
        c["status"] = "adopted"
        c["adopted_by"] = "演示切片（非真实采纳记录）"
        p = prev.get(c["id"])
        same = p and {k: v for k, v in p.items() if k != "adopted_at"} == c
        c["adopted_at"] = p["adopted_at"] if same else today
    dump("clauses.demo.yaml", {"clauses": clauses})
    dump("links.yaml", {"links": links})
    dump("conflicts.yaml", {"conflicts": confs})
    dump("golden_cases.yaml", {"cases": cases})
    # 停用词表整份带走：它是**机制**（人工维护的「idf」，不随语料增长自动漂），
    # 里面没有条款判断，正是 该项裁决 想公开的那一类东西。
    # 条目也走白名单（2026-10-06，仓库迁移方案第 6.3 节 该项裁决）：`evidence` 是逐用例的实测记录，
    # 引了未导出用例的提问原文与切片外条款 id，等于把用例集带出去。运行时只读 `term`。
    stop = yaml.safe_load((K / "stopwords.yaml").read_text(encoding="utf-8"))
    entry = {"stopwords_zh": STOP_FIELDS, "stopwords_en": STOP_FIELDS,
             "never_stopword": NEVER_STOP_FIELDS}
    out = {k: stop[k] for k in ("meta", "rule") if k in stop}
    for k, fields in entry.items():
        if k in stop:
            out[k] = [pick(w, fields) for w in stop[k] or []]
    dump("stopwords.yaml", out)
    print(f"条款 {len(clauses)} · 链接 {len(links)} · 冲突 {len(confs)} · 用例 {len(cases)} → {OUT}")


if __name__ == "__main__":
    main()
