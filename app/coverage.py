"""覆盖范围声明：`coverage_scope_zh`。

`copy_disclaimer.md` 第 7、8 节要用它。**2026-09-06 owner 拍板改为按 `clause` 表
动态生成，不再写死条号**——理由是 A1 之后每采纳一批，写死的那句就会把已覆盖的
问题拒答掉，A2 采纳当天就撞上了。

怎么生成是实现细节，但**生成结果必须满足第 7 节的四条产品约束**：

  一、只数 `status = 'adopted'`——草稿不构成覆盖，说了就是对用户虚报；
  二、**部分覆盖的条不得整条声明**（见 PARTIAL_ARTICLES）；
  三、附件按登记粒度如实说；
  四、与 `corpus_version` 绑定，**不得缓存成常量**。

第二条是这里唯一容易做错的地方，而做错的代价按第 7 节原话是
「红线第 6 条『把未覆盖当作不适用』的镜像形态」。
"""

from __future__ import annotations

from app.config import KNOWLEDGE_DIR

#: 只登记了部分款的条：**要么不列入覆盖声明，要么写明只覆盖哪几款**。
#:
#: 它们进索引是因为别的单元指向了它们，不是因为我们覆盖了那条。
#: 清单正本在 `copy_disclaimer.md` 第 7 节的表格里，这里是镜像——
#: **`tests/test_coverage.py` 会拿那份文档交叉核对，漂了就红**。
#: 产品侧新采纳一批之后，若有新的部分覆盖条，改文档即可，测试会提醒开发同步。
#: 部分覆盖的条：只登记了个别款，**不得整条声明**。
#:
#: **不写死在代码里**——它是索引侧的事实、随采纳进度变，写进源码等于把
#: 「我们覆盖到哪儿」印在代码上。放索引目录的 `coverage.yaml`，缺文件即空集。
def _partial_articles() -> dict[str, str]:
    import yaml

    f = KNOWLEDGE_DIR / "coverage.yaml"
    if not f.exists():
        return {}
    doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in (doc.get("partial_articles") or {}).items()}


PARTIAL_ARTICLES = _partial_articles()

#: 附件的登记粒度限定（第七节第三条：按登记粒度如实说）。
ANNEX_CAVEATS: dict[str, str] = {}     # 同上，由 coverage.yaml 提供


def _fmt_articles(nums: list[str]) -> str:
    """连续条号合并成区间，读起来短一些。「第 5、15 至 17、38 条」。"""
    if not nums:
        return ""
    ints = sorted(int(n) for n in nums)
    runs: list[tuple[int, int]] = []
    for n in ints:
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    parts = [f"{a}" if a == b else (f"{a}、{b}" if b == a + 1 else f"{a} 至 {b}")
             for a, b in runs]
    return "第 " + "、".join(parts) + " 条"


def coverage_scope_zh(conn) -> str:
    """按库里**已采纳**的条款生成覆盖范围短语。

    每次调用都重算——第四条约束要求它与 `corpus_version` 绑定，不得缓存成常量。
    """
    rows = conn.execute(
        """
        select c.source_id, c.path_article, c.annex, count(*)
        from clause c join source s on s.id = c.source_id
        where c.status = 'adopted' and s.rights_status = 'ALLOW'
        group by 1, 2, 3
        """
    ).fetchall()

    full_arts: list[str] = []
    partial: list[str] = []
    annexes: set[str] = set()
    has_guidance = has_faq = has_delegated = False

    for source_id, article, annex, _n in rows:
        if source_id == "ppwr_guidance":
            has_guidance = True
        elif source_id == "ppwr_faq":
            has_faq = True
        elif source_id != "ppwr_reg":
            has_delegated = True
        elif annex:
            annexes.add(annex.split("/")[0])
        elif article:
            # 约束二：部分覆盖的条**必须**带上限定，不得整条声明
            if article in PARTIAL_ARTICLES:
                partial.append(f"第 {article} 条（{PARTIAL_ARTICLES[article]}）")
            else:
                full_arts.append(article)

    parts: list[str] = []
    if full_arts:
        parts.append(f"PPWR {_fmt_articles(full_arts)}")
    if partial:
        parts.append("、".join(sorted(partial, key=lambda s: int(s[2:s.index(" 条")]))))
    if annexes:
        # 约束三：附件按登记粒度如实说
        items = []
        for a in sorted(annexes):
            cav = ANNEX_CAVEATS.get(a)
            items.append(f"附件 {a}（{cav}）" if cav else f"附件 {a}")
        parts.append("、".join(items))

    tail = []
    if has_guidance:
        tail.append("官方实施指南")
    if has_faq:
        tail.append("FAQ")
    if has_delegated:
        tail.append("相关授权决定")
    if tail:
        parts.append("以及与之相关的" + "、".join(tail))

    return "、".join(parts) if parts else "（当前索引为空）"
