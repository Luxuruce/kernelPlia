"""该任务：把四份 PDF 一次性预渲染为页图。

产品契约文档 第二节「bbox 与页图约定」：

> 页图由 `pypdfium2` **一次性预渲染**为 PNG（232 页），静态托管，
> 命名规则 `{source_id}/p{page}.png`。运行时不做渲染。
> **前端不使用 PDF.js**——预渲染绕开了欧盟公报 PDF 的字体加载问题，也少一个大依赖。

**渲染分辨率与 bbox 无关**：bbox 是归一化的 0..1，前端按图片实际显示尺寸乘上去即可，
换分辨率不需要重算 bbox。所以这里的 SCALE 可以随时调，不影响已录入的坐标。

    ~/.venvs/kernel/bin/python scripts/render_pages.py           # 只渲染缺的
    ~/.venvs/kernel/bin/python scripts/render_pages.py --force   # 全部重渲
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pypdfium2 as pdfium

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import CORPUS_ROOT, IMAGE_EXT, PAGE_IMAGE_DIR  # noqa: E402
from ingest.corpus import CorpusChanged, verify_corpus  # noqa: E402

# source_id → PDF 相对 CORPUS_ROOT 的路径。
# 与 ingest/corpus.py 的 SOURCE_KEY 一一对应，四份来源齐。
SOURCE_PDF = {
    "ppwr_reg": "01_PPWR法规原文_Regulation-EU-2025-40_EN.pdf",
    "ppwr_guidance": "02_PPWR官方实施指南_OJ-C-2026-3084_EN.pdf",
    "ppwr_faq": "03_PPWR_FAQ_第二版_2026-08_EN.pdf",
    "dec_2026_429": "04_授权与实施法案/01_授权决定_EU-2026-429_托盘缠绕膜与捆扎带_EN.pdf",
}

# 渲染倍率。2.0 ≈ 144 dpi，条文正文在普通屏上足够清晰。
# 调它不影响 bbox（归一化坐标）。
SCALE = 2.0

# 输出 **无损 WebP**，不是 PNG。
#
# 这几页是渲染出来的文字页、大片纯白，WebP 的无损压缩比 PNG 强得多：
# 实测同一页同一倍率 **452 KB → 90 KB**，全库 **86 MB → 22 MB**，
# 而且 `ImageChops.difference` 逐像素比对为 `None`——**一点画质都没损**。
#
# 之所以要压：Vercel Hobby 的源文件上传上限是 100 MB，86 MB 的 PNG 贴着线，
# 加上代码和语料就超了。降分辨率是另一条路，但没必要——无损压缩已经够。
IMAGE_SAVE = {"format": "WEBP", "lossless": True, "method": 6}   # 后缀见 config.IMAGE_EXT


def render(source_id: str, pdf_path: pathlib.Path, force: bool) -> tuple[int, int]:
    out_dir = PAGE_IMAGE_DIR / source_id
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        n = len(doc)
        written = 0
        for i in range(n):
            # 页码按 1 基，与 clause.page_from / bbox 的 page 一致，也与 PDF 显示页码一致
            target = out_dir / f"p{i + 1}.{IMAGE_EXT}"
            if target.exists() and not force:
                continue
            page = doc[i]
            page.render(scale=SCALE).to_pil().save(target, **IMAGE_SAVE)
            page.close()
            written += 1
        return n, written
    finally:
        doc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="预渲染 PDF 页图（该任务）")
    ap.add_argument("--force", action="store_true", help="已存在的也重渲")
    args = ap.parse_args()

    # 页图是 PDF 的派生物，PDF 变了页图就过期。渲染前校验语料版本，
    # 与装载同一道闸门（MANIFEST.md 第 2 节）。
    try:
        verify_corpus()
    except CorpusChanged as e:
        print(f"✗ 语料校验不通过，拒绝渲染：\n{e}")
        return 1

    total_pages = total_written = 0
    for source_id, rel in SOURCE_PDF.items():
        pdf = CORPUS_ROOT / rel
        if not pdf.exists():
            print(f"✗ 找不到 {pdf}")
            return 1
        n, written = render(source_id, pdf, args.force)
        total_pages += n
        total_written += written
        print(f"  {source_id:<14} {n:>4} 页" + (f"，新渲 {written}" if written else "，已是最新"))

    size = sum(p.stat().st_size for p in PAGE_IMAGE_DIR.rglob("*.png"))
    print(f"\n✓ 共 {total_pages} 页，本次渲染 {total_written} 张")
    print(f"  {PAGE_IMAGE_DIR}  合计 {size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
