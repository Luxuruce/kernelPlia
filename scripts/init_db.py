"""建库：应用 schema.sql 与 seed_sources.sql。

对应 V0_验收标准文档 执行序 S0（该任务 索引契约落地）。
幂等——两个 SQL 文件都写成可重复执行。

    ~/.venvs/kernel/bin/python scripts/init_db.py
"""

import pathlib
import sys

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

DB_DIR = pathlib.Path(__file__).resolve().parent.parent / "db"


def main() -> int:
    with psycopg.connect(settings.database_url) as conn:
        for name in ("schema.sql", "seed_sources.sql"):
            sql = (DB_DIR / name).read_text(encoding="utf-8")
            conn.execute(sql)
            conn.commit()
            print(f"已应用 {name}")

        tables = conn.execute(
            """
            select table_name from information_schema.tables
            where table_schema = 'public' order by table_name
            """
        ).fetchall()
        print("表：", ", ".join(t[0] for t in tables))

        sources = conn.execute(
            "select id, authority_rank, rights_status from source order by authority_rank"
        ).fetchall()
        print("K1 来源登记：")
        for sid, rank, rights in sources:
            print(f"  位阶 {rank}  {sid:<14} rights={rights}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
