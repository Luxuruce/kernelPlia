"""提取件与 bbox 边车的读取。

装载约定一与二（`knowledge/ppwr/README.md` 第 2 节）：
    text_en 不在 YAML 里，由行号从提取件取；
    bbox 不在 YAML 里，由同一组行号 join 坐标边车取。

英文是作准文本，提取件是它的唯一正本。抄一遍进 YAML 只会引入转录误差。
提取件与边车都是**只读外部输入**，本模块只读不写。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import CHECKSUMS_FILE, CORPUS_DIR, CORPUS_ROOT

# source_id → 提取件文件名主干
SOURCE_KEY = {
    "ppwr_reg": "01_regulation",
    "ppwr_guidance": "02_guidance",
    "ppwr_faq": "03_faq",
    "dec_2026_429": "04_dec_2026_429",
}

# 装载约定五：04_dec_2026_429 不在 extract.py 的 FILES 里，没有坐标边车。
# 引用它只能给页码不能高亮，前端须降级显示而不是报错。
NO_BBOX_SOURCES = {"dec_2026_429"}

# 页码标记行，形如 [[p39]]。它不是条文，取文时剔除；
# 边车里也没有它的坐标，join 不到属正常，不得报错（装载约定二）。
PAGE_MARKER = re.compile(r"^\[\[p\d+\]\]$")


class CorpusChanged(Exception):
    """语料与冻结基线不一致。**不得降级为警告继续装载。**"""


def verify_corpus() -> int:
    """校验语料副本与 `CHECKSUMS.sha256` 的冻结基线一致，返回校验通过的文件数。

    `MANIFEST.md` 第 2 节：装载前校验一次，不一致即停止装载并报错，
    **不要「先装了再说」**。

    理由是这类损坏**不会自己暴露**：txt 行号是 63 个条款单元 `line_from/line_to`
    的锚，行号一漂移，装载照样成功、测试照样绿，只是每一条引用都悄悄指向了
    错误的原文段落——而引用正确性正是本产品唯一的护城河（规范原则一）。

    校验整个副本而不只是 `_text_clean/`：PDF 是页图预渲染（该任务）的输入，
    换了 PDF 而不换提取件，高亮框就会画在错的位置上。
    """
    if not CHECKSUMS_FILE.exists():
        raise CorpusChanged(f"找不到冻结基线 {CHECKSUMS_FILE}——无法确认语料版本，拒绝装载")

    bad: list[str] = []
    n = 0
    for line in CHECKSUMS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        want, _, rel = line.partition("  ")
        path = CORPUS_ROOT / rel
        if not path.exists():
            bad.append(f"{rel}：文件不存在")
            continue
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            bad.append(f"{rel}：哈希不符（基线 {want[:12]}… 实得 {h.hexdigest()[:12]}…）")
        n += 1

    if bad:
        raise CorpusChanged(
            f"语料副本与冻结基线不一致，{len(bad)} 项：\n  "
            + "\n  ".join(bad)
            + f"\n基线文件：{CHECKSUMS_FILE}\n"
            "改语料的正确做法见 MANIFEST.md 第 6 节：改完更新 CHECKSUMS.sha256 "
            "并递增 corpus_version，不要绕过校验。"
        )
    return n


@dataclass
class Extract:
    key: str
    lines: list[str]                  # 1 基行号 → lines[n - 1]
    bbox_by_line: dict[int, dict]     # line → {page, x0, y0, x1, y1}

    @property
    def n_lines(self) -> int:
        return len(self.lines)


@lru_cache(maxsize=None)
def load_extract(source_id: str) -> Extract:
    """读一份提取件与它的边车，按 source_id 缓存。"""
    key = SOURCE_KEY[source_id]
    lines = (CORPUS_DIR / f"{key}.txt").read_text(encoding="utf-8").splitlines()

    bbox_by_line: dict[int, dict] = {}
    if source_id not in NO_BBOX_SOURCES:
        with open(CORPUS_DIR / f"{key}.bbox.jsonl", encoding="utf-8") as f:
            for raw in f:
                r = json.loads(raw)
                bbox_by_line[r["line"]] = {
                    "page": r["page"],
                    "x0": r["x0"], "y0": r["y0"],
                    "x1": r["x1"], "y1": r["y1"],
                }
    return Extract(key=key, lines=lines, bbox_by_line=bbox_by_line)


def text_en_from_lines(
    source_id: str, line_from: int, line_to: int, exclude_lines: list[int] | None = None
) -> str:
    """装载约定一：按行号区间取作准文本。

    去掉 [[pN]] 页码标记行与 exclude_lines 所列行，按空格拼接。

    exclude_lines 的来源是欧盟公报的排版——脚注会被夹在正文段落中间，
    按区间取文会一并取进来。把脚注框进高亮不算错，把它读成条文才算错。
    """
    ex = set(exclude_lines or ())
    ext = load_extract(source_id)
    out = []
    for n in range(line_from, line_to + 1):
        if n in ex or n > ext.n_lines:
            continue
        line = ext.lines[n - 1].strip()
        if not line or PAGE_MARKER.match(line):
            continue
        out.append(line)
    return " ".join(out)


def bbox_from_lines(source_id: str, line_from: int, line_to: int) -> list[dict]:
    """装载约定二：按同一组行号 join 坐标边车。

    每行一个矩形，**不合并成大包围盒**——合并会框住无关正文。
    join 不到坐标的行直接跳过不报错：那是 [[pN]] 页码标记行，本来就不该有框。
    这类行不止一处（1873、2766、指南与 FAQ 各有），不要按「只有一个断点」写死。

    **exclude_lines 不在这里生效**（装载约定三）：高亮框仍按完整区间画，
    因为脚注在页面上确实在那个位置。
    """
    ext = load_extract(source_id)
    return [
        {"line": n, **ext.bbox_by_line[n]}
        for n in range(line_from, line_to + 1)
        if n in ext.bbox_by_line
    ]
