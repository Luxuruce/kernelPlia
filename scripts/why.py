"""为什么是这些条款？——命中词转储。

这一轮（2026-09-06）两次归因都是靠它定的：`包装重` 跨词伪词误触发 C2、
`PPWR` 四个字母单独越过冲突闸门。两次都是现写脚本查的，
产研边界约定 7quinquies 待办把它列为**开发侧要固化的常驻调试输出**——
所以它在这里，不再每次现写。

    ~/.venvs/kernel/bin/python scripts/why.py "我的包装重金属超标了会怎样？"
    ~/.venvs/kernel/bin/python scripts/why.py "…" --clause ppwr/art/29/1

**只读**，不写库、不调模型、不花钱。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.context import assemble, estimate_tokens, fit_to_budget, plan_trim, serialize
from app.db import connect
from app.retrieval import TRIGGER_SCORE_THRESHOLD, retrieve, self_scores
from app.terms import extract_terms

#: 与 retrieval.TRIGGER_SCORE_SQL 的字段权重一致。**改了那边要改这边**，
#: 否则这份转储会理直气壮地报一个假的分数。
FIELD_WEIGHT = (("heading", 3.0), ("text_zh", 2.0), ("text_en", 1.5))


def hits_for(conn, terms: dict[str, float], clause_id: str) -> list[tuple[float, str, str]]:
    """某条款被哪些词、在哪个字段命中，各得几分。按 `retrieval` 的口径：
    **一个词只算它命中的最高权重字段**，取所有词里的最大值。"""
    row = conn.execute(
        "select heading, text_zh, text_en from clause where id = %s", (clause_id,)
    ).fetchone()
    if row is None:
        return []
    fields = dict(zip(("heading", "text_zh", "text_en"), row))
    out = []
    for t, w in terms.items():
        for name, weight in FIELD_WEIGHT:
            if fields.get(name) and t in fields[name]:
                out.append((w * weight, t, name))
                break
    out.sort(reverse=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--clause", action="append", default=[],
                    help="额外拆解这些条款的命中明细（可重复）")
    a = ap.parse_args()

    terms = extract_terms(a.question)
    print(f"提问　{a.question}")
    print(f"候选词（{len(terms)}）", " ".join(sorted(terms, key=len)))

    with connect() as conn:
        r = retrieve(conn, a.question)

        print(f"\n召回 {len(r.hits)} 条")
        for h in r.hits:
            print(f"  {h.origin:<16} {h.clause_id:<28} {h.via or ''}")

        # 冲突闸门：**只看触发条款自身的自足分数**，与命中集无关（该项改动）
        rows = conn.execute(
            "select id, trigger_clause_ids, trigger_context_terms from conflict order by id"
        ).fetchall()
        trig = sorted({t for _, ts, _ in rows for t in ts})
        sc = self_scores(conn, terms, trig)
        print(f"\n冲突闸门（阈值 {TRIGGER_SCORE_THRESHOLD}）")
        for cid, ts, ctx_terms in rows:
            best = max(((sc.get(t, 0.0), t) for t in ts), default=(0.0, None))
            gated = bool(ctx_terms) and not any(x in a.question for x in ctx_terms)
            mark = "语境词未命中" if gated else ("触发" if best[0] >= TRIGGER_SCORE_THRESHOLD else "")
            print(f"  {cid:<34} {best[0]:6.2f}  {best[1] or '':<24} {mark}")
            if best[1] and best[0] > 0:
                for s, t, f in hits_for(conn, terms, best[1])[:4]:
                    print(f"      {s:6.2f}  {t:<12} {f}")
        print(f"  → 实际阻断 {r.conflict_ids or '无'}"
              f" · 伴随 {r.accompanying_conflict_ids or '无'}")

        items = assemble(conn, r)
        kept, dropped = fit_to_budget(items)
        order, rep = plan_trim(items)
        print(f"\n上下文 {len(kept)}/{len(items)} 条 · {estimate_tokens(serialize(kept))} token")
        kept_ids = {i.data["id"] for i in kept}
        for n, (key, ids) in enumerate(order):
            tag = "保" if ids[0] in kept_ids else "丢"
            head, *rest = ids
            bind = ("  ⟷ " + " ⟷ ".join(rest)) if rest else ""
            print(f"  {tag} #{n:<3} rank={key[0]}  {head:<28}{bind}")

        for cid in a.clause:
            print(f"\n{cid} 的命中明细")
            for s, t, f in hits_for(conn, terms, cid):
                print(f"  {s:6.2f}  {t:<12} {f}")


if __name__ == "__main__":
    main()
