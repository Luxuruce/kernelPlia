"""测试夹具。

**两种模式，按 `KNOWLEDGE_DIR` 指向哪套索引自动判定**（仓库迁移方案第 6.1 节）：

- **完整索引**（本仓默认，`knowledge/ppwr/`）：全部用例都跑；
- **演示切片**（公开仓默认，`demo_index/`，10 条条款单元）：标了 `full_index` 的用例跳过——
  它们点名了切片外的条款、冲突 某条冲突/某条冲突、页图或完整索引上的实测基线，在切片上没有意义。

**目录即发布边界**（2026-10-06 裁决 该项裁决，仓库迁移方案第 6.3 节）：`tests/full/` 放依赖完整索引的用例，
它们写着用例集的提问与期望引用，**不发布**；`tests/` 根下的随代码发布，公开仓发布前要在演示切片上
全绿（`tools/publish_public.sh` 闸门四）。**`full_index` 标记只许出现在 `tests/full/` 里**，闸门三会查。
本地跑演示模式：

    KNOWLEDGE_DIR=demo_index DATABASE_URL=…/kernel_demo python -m ingest.load
    KNOWLEDGE_DIR=demo_index DATABASE_URL=…/kernel_demo python -m pytest tests -q

**2026-09-05 审核记录已签字**（`adopted_by: L`），全批 63 条已由 `draft` 翻为 `adopted`，
检索在真实库上就能出结果了。

`adopted_conn` 仍然保留，理由有二：一是让检索回归**不依赖当前的采纳状态**——
下一批条款录入时又会是 draft，测试不该因此变红；二是它在事务内做、跑完回滚，
不会污染库。**生产代码路径里没有任何绕过采纳闸门的开关，也不该有。**
"""

from __future__ import annotations

import pathlib
import sys

import psycopg
import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import KNOWLEDGE_DIR, PAGE_IMAGE_DIR, settings  # noqa: E402

#: 演示切片的标志文件。导出器只写 `clauses.demo.yaml`，完整索引里没有这个名字。
DEMO_INDEX = (KNOWLEDGE_DIR / "clauses.demo.yaml").is_file()

#: 有没有预渲染页图。本仓提交了（Vercel 要发 CDN），公开仓不带（也没有 PDF 可渲染）。
HAS_PAGES = PAGE_IMAGE_DIR.is_dir() and any(PAGE_IMAGE_DIR.glob("*/p1.*"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "full_index: 依赖完整索引，演示切片上跳过")
    config.addinivalue_line(
        "markers", "page_images: 依赖预渲染页图，没有页图时跳过")


def pytest_collection_modifyitems(config, items):
    no_index = pytest.mark.skip(
        reason="依赖完整索引：点名了演示切片外的条款、冲突、页图或实测基线")
    no_pages = pytest.mark.skip(
        reason="没有预渲染页图（公开仓不带；有 PDF 时跑 scripts/render_pages.py 生成）")
    for item in items:
        if DEMO_INDEX and "full_index" in item.keywords:
            item.add_marker(no_index)
        if not HAS_PAGES and "page_images" in item.keywords:
            item.add_marker(no_pages)


@pytest.fixture(scope="session")
def golden_cases() -> list[dict]:
    doc = yaml.safe_load(
        (KNOWLEDGE_DIR / "golden_cases.yaml").read_text(encoding="utf-8")
    )
    return doc["cases"]


@pytest.fixture(scope="session")
def cases(golden_cases) -> list[dict]:
    """`golden_cases` 的别名，演示切片那批用例用这个名字。"""
    return golden_cases


@pytest.fixture(scope="session")
def conn():
    """只读连接。库里没有已采纳条款就跳过——先装载再测。"""
    with psycopg.connect(settings.database_url) as c:
        n = c.execute("select count(*) from clause where status = 'adopted'").fetchone()[0]
        if n == 0:
            pytest.skip("库里没有已采纳条款，先跑 python -m ingest.load")
        yield c


@pytest.fixture(scope="module")
def adopted_conn():
    """事务内确保全批为 adopted，跑完回滚。

    全批已采纳时这是个空操作；下一批条款进来还是 draft 时它兜住，
    让检索回归不随采纳进度飘。"""
    with psycopg.connect(settings.database_url) as conn:
        n = conn.execute("select count(*) from clause").fetchone()[0]
        if n == 0:
            pytest.skip("库里没有条款，先跑 python -m ingest.load")
        with conn.transaction() as tx:
            conn.execute(
                """update clause
                   set status = 'adopted',
                       adopted_by = '回归测试夹具（非真实采纳）',
                       adopted_at = now()"""
            )
            yield conn
            raise psycopg.Rollback(tx)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个用例前清掉 TestClient 那个 IP 的当日计数。

    该项裁决 的每 IP 每日 5 次是**真闸门**，跑在测试里也一样拦——
    一个测试文件里几次 `/api/ask` 就会把额度用光，后面的用例全被降级。
    这里只清 `testclient` 一个 IP：**闸门本身不绕过**，
    绕过就等于没在测它（限流的专项用例见 test_api.py）。
    """
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("delete from rate_limit where ip in ('testclient', 'unknown')")
        conn.commit()
    yield
