"""S0 完成判据验证。

判据（V0_验收标准文档「关键路径、执行顺序与闸门」）：
    四张表建成并可执行；能手工插入一条第 5(4) 记录并按 clause_id 查出。

顺带验证三条治理约束真的在数据库层生效，而不是只写在注释里：
    1. status='adopted' 必须具名（红线第 2 条：不得由 AI 单独批准规范断言）
    2. authority_rank 只能是 1..4（规范 5.2 四级位阶）
    3. rights_status 只能是 ALLOW/DENY/UNKNOWN（规范 6.4 权利闸）

本脚本**不留下数据**：插入的第 5(4) 记录在校验后回滚。
真正的第 5 条录入是 S2（该任务），由专家采纳并具名，不该由冒烟测试代劳。

    ~/.venvs/kernel/bin/python scripts/smoke_s0.py
"""

import json
import pathlib
import sys

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.config import CORPUS_DIR, settings  # noqa: E402

# 第 5(4) 条：01_regulation.txt 第 1862–1865 行，PDF 第 38 页
ART_5_4_LINES = (1862, 1865)

ok, fail = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (ok if cond else fail).append(label)
    print(f"  {'✓' if cond else '✗'} {label}{'  ' + detail if detail else ''}")


def load_text_and_bbox() -> tuple[str, list[dict]]:
    """从提取件与 bbox 边车取第 5(4) 的原文与高亮框，验证 S1 产物直通 S0。"""
    lo, hi = ART_5_4_LINES
    lines = (CORPUS_DIR / "01_regulation.txt").read_text(encoding="utf-8").splitlines()
    text_en = " ".join(lines[lo - 1 : hi])

    bbox = []
    with open(CORPUS_DIR / "01_regulation.bbox.jsonl", encoding="utf-8") as f:
        for raw in f:
            r = json.loads(raw)
            if lo <= r["line"] <= hi:
                bbox.append(
                    {"page": r["page"], "x0": r["x0"], "y0": r["y0"],
                     "x1": r["x1"], "y1": r["y1"]}
                )
    return text_en, bbox


def main() -> int:
    text_en, bbox = load_text_and_bbox()

    print("== 语料侧（S1 产物）==")
    check("第 5(4) 原文取到", "100 mg/kg" in text_en, f"{len(text_en)} 字符")
    check("高亮框取到", len(bbox) == 4, f"{len(bbox)} 行，第 {bbox[0]['page']} 页")
    check(
        "坐标归一化在 0..1",
        all(0 <= b[k] <= 1 for b in bbox for k in ("x0", "y0", "x1", "y1")),
    )

    with psycopg.connect(settings.database_url) as conn:
        print("\n== 四张表 ==")
        for t in ("source", "clause", "clause_link", "conflict"):
            n = conn.execute(f"select count(*) from {t}").fetchone()[0]
            check(f"{t} 可查", True, f"{n} 行")

        print("\n== K1 来源登记（该任务）==")
        rows = conn.execute(
            "select id, authority_rank, rights_status from source order by authority_rank"
        ).fetchall()
        check("四份来源齐", len(rows) == 4)
        check(
            "位阶 1..4 各一份",
            sorted(r[1] for r in rows) == [1, 2, 3, 4],
            str([(r[0], r[1]) for r in rows]),
        )
        check("全部 rights_status=ALLOW", all(r[2] == "ALLOW" for r in rows))

        print("\n== 插入并查回第 5(4) ==")
        with conn.transaction() as tx:
            conn.execute(
                """
                insert into clause (id, source_id, path_article, path_paragraph,
                                    heading, text_en, page_from, page_to,
                                    line_from, line_to, bbox, scope,
                                    effective_expr, numeric_limits, status)
                values (%s, 'ppwr_reg', '5', '4',
                        'Requirements for substances in packaging', %s, 38, 38,
                        %s, %s, %s, 'all_packaging', %s, %s, 'draft')
                """,
                (
                    "smoke/ppwr/art/5/4",
                    text_en,
                    ART_5_4_LINES[0],
                    ART_5_4_LINES[1],
                    json.dumps(bbox),
                    json.dumps({"kind": "point", "date": "2026-08-12"}),
                    json.dumps(
                        [
                            {
                                "name": "lead+cadmium+mercury+hexavalent chromium (sum)",
                                "value": 100,
                                "unit": "mg/kg",
                                "basis": "limit",
                            }
                        ]
                    ),
                ),
            )
            got = conn.execute(
                """
                select c.id, c.scope, c.status, c.line_from, c.line_to,
                       jsonb_array_length(c.bbox), c.numeric_limits,
                       s.authority_rank, s.rights_status
                from clause c join source s on s.id = c.source_id
                where c.id = %s
                """,
                ("smoke/ppwr/art/5/4",),
            ).fetchone()

            check("按 clause_id 查回", got is not None)
            check("scope=all_packaging", got[1] == "all_packaging", "5(4) 适用全部包装")
            check("行号区间落库", (got[3], got[4]) == ART_5_4_LINES, f"{got[3]}–{got[4]}")
            check("bbox 4 个矩形", got[5] == 4)
            check("限值 100 mg/kg", got[6][0]["value"] == 100)
            check("join 得到位阶与权利状态", (got[7], got[8]) == (1, "ALLOW"))

            print("\n== 治理约束是否真的在数据库层生效 ==")
            try:
                conn.execute(
                    "update clause set status='adopted' where id=%s",
                    ("smoke/ppwr/art/5/4",),
                )
                check("adopted 未具名被拒", False, "约束没拦住，红线第 2 条失守")
            except psycopg.errors.CheckViolation:
                check("adopted 未具名被拒", True)

            # psycopg3 的事务靠抛 Rollback 回滚。冒烟测试不留数据，真正录入是 S2
            raise psycopg.Rollback(tx)

        for bad_sql, label in (
            ("insert into source values ('x','x','regulation',9,null,null,'1',"
             "'ORIGINAL_AUTHENTIC','ALLOW',now())", "位阶越界被拒"),
            ("insert into source values ('y','y','regulation',1,null,null,'1',"
             "'ORIGINAL_AUTHENTIC','MAYBE',now())", "非法 rights_status 被拒"),
        ):
            try:
                with conn.transaction() as tx:
                    conn.execute(bad_sql)
                    raise psycopg.Rollback(tx)
                check(label, False, "约束没拦住")
            except psycopg.errors.CheckViolation:
                check(label, True)

        left = conn.execute("select count(*) from clause").fetchone()[0]
        print(f"\n冒烟数据已回滚，clause 表剩 {left} 行")

    print(f"\n{'=' * 46}\nS0 判据：通过 {len(ok)} 项，失败 {len(fail)} 项")
    if fail:
        print("失败：" + "、".join(fail))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
