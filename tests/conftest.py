"""测试夹具。

这些测试跑在**仓内的演示切片**上（`demo_index/`，10 条条款单元），
不依赖任何私有内容。装载一次到 `DATABASE_URL` 指的库即可：

    docker compose up -d
    python scripts/init_db.py && python -m ingest.load
    pytest -q
"""

from __future__ import annotations

import pathlib
import sys

import psycopg
import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import KNOWLEDGE_DIR, settings  # noqa: E402


@pytest.fixture(scope="session")
def conn():
    with psycopg.connect(settings.database_url) as c:
        n = c.execute("select count(*) from clause where status = 'adopted'").fetchone()[0]
        if n == 0:
            pytest.skip("库里没有已采纳条款，先跑 python -m ingest.load")
        yield c


@pytest.fixture(scope="session")
def cases() -> list[dict]:
    return yaml.safe_load(
        (KNOWLEDGE_DIR / "golden_cases.yaml").read_text(encoding="utf-8")
    )["cases"]


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个用例前清掉 TestClient 那个 IP 的当日计数。

    每 IP 每日 5 次是**真闸门**，跑在测试里也一样拦——一个文件里几次
    `/api/ask` 就会把额度用光。这里只清那一个 IP，**闸门本身不绕过**。
    """
    with psycopg.connect(settings.database_url) as c:
        c.execute("delete from rate_limit where ip in ('testclient', 'unknown')")
        c.commit()
    yield
