"""覆盖范围声明（`copy_disclaimer.md` 第 7 节）。

部分覆盖清单在索引目录的 `coverage.yaml`，正本是 `copy_disclaimer.md` 第 7 节的表格。
两者的交叉核对在 `tests/full/test_coverage_full.py`（公开仓不带 `copy_disclaimer.md`）。
"""

from __future__ import annotations

import pytest
import yaml

from app import coverage


def _mirror() -> dict:
    return yaml.safe_load(coverage.COVERAGE_FILE.read_text(encoding="utf-8")) or {}


def test_部分覆盖的条不得整条声明(conn):
    """红线第 6 条「把未覆盖当作不适用」的镜像形态：整条声明会让用户以为能问那条的任何一款。"""
    scope = coverage.coverage_scope_zh(conn)
    arts = {r[0] for r in conn.execute(
        "select distinct path_article from clause "
        "where status = 'adopted' and source_id = 'ppwr_reg' and annex is null")}
    for art, limit in _mirror()["partial_articles"].items():
        if art in arts:
            assert f"第 {art} 条（{limit}）" in scope, f"第 {art} 条被整条声明了：{scope}"


def test_缺镜像文件直接报错_不退化成空集(conn, monkeypatch, tmp_path):
    """空集的意思是「没有部分覆盖的条」——缺文件若按空集走，第 29 条就会被整条声明。"""
    monkeypatch.setattr(coverage, "COVERAGE_FILE", tmp_path / "coverage.yaml")
    with pytest.raises(FileNotFoundError):
        coverage.coverage_scope_zh(conn)
