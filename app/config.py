"""运行配置。

密钥一律走环境变量或 .env，不入库、不进代码（.env 已在仓库根 .gitignore 中）。
"""

import os
import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 语料：**本项目自有副本**，不是上游 solutions/PPWR/ 的活指针。
#
# 红线第 4 条要求「正式执行时使用固定版本的规范发布集」，规范 6.3 第一条要求
# 「答案绑定语料版本」——指向共享工作目录的活指针不满足「固定版本」，
# 上游的全译工程仍在变动那个目录。所以 2026-09-05 复制 + 哈希锁定，把版本钉死。
#
# 提取件与 bbox 边车对 kernel 是只读输入。**装载前必须校验 CHECKSUMS.sha256**
# （见 ingest/corpus.py 的 verify_corpus）：txt 行号是 63 个条款单元
# line_from/line_to 的锚，漂移不会报错，只会让引用悄悄指错地方。
#: 内容正本目录（条款 YAML、黄金用例、免责文案）。产品侧维护，`kernel/` 只读。
#
# **`kernel/` 里对内容正本的路径依赖全部收敛在这里。** 代码与内容分属两侧：
# 代码是通用的检索与出口纪律，内容是 PPWR 的结构化索引——后者才是护城河
# （「护城河是索引不是代码」这一判断）。留一个环境变量，将来代码独立成库时不必改一行。
# 公开仓自带的是 **10 条条款单元的演示切片**（见 demo_index/README.md），
# 够跑通全部机制，不是完整索引。真实索引在另一侧，用 KNOWLEDGE_DIR 指过去即可。
KNOWLEDGE_DIR = pathlib.Path(os.getenv("KNOWLEDGE_DIR", ROOT / "demo_index")).resolve()

CORPUS_ROOT = pathlib.Path(
    os.getenv("CORPUS_ROOT", KNOWLEDGE_DIR / "EUoffical")
).resolve()
CORPUS_DIR = CORPUS_ROOT / "_text_clean"
CHECKSUMS_FILE = CORPUS_ROOT / "CHECKSUMS.sha256"

# 该任务 预渲染页图。**PDF 的派生物，不是内容正本**——所以落在 kernel/ 下、不入库、
# 可随时由 scripts/render_pages.py 重建，`.gitignore` 已排除。
# 命名规则 {source_id}/p{page}.png（产品契约文档 第二节「bbox 与页图约定」）。
PAGE_IMAGE_DIR = pathlib.Path(
    os.getenv("PAGE_IMAGE_DIR", ROOT / "static" / "pages")
).resolve()
# 前端取页图的 URL 前缀。静态托管，运行时不做渲染。
PAGE_IMAGE_URL_PREFIX = os.getenv("PAGE_IMAGE_URL_PREFIX", "/static/pages")


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://kernel:kernel_dev_only@localhost:55432/kernel"
    )
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")

    # 产品契约文档 第一节技术栈定稿
    # 2026-09-06 owner 拍板：**lite 优先，只有 lite 到达限额才转用 pro。**
    #
    # 依据是延迟专项实测（scripts/gate_ark_latency.py）：
    #   lite 默认   33.4s · ¥0.011 · 输出 1480 tok
    #   pro  默认  245.5s · ¥0.478 · 输出 13896 tok（绝大部分是思考流）
    # pro 默认配置的总耗时与成本都不适合放在主路径上。
    #
    # 火山**没有服务端 fallbacks**，所以回退是客户端做的，
    # 由 providers.classify() 的错误分层驱动（见 synthesize_with_fallback）。
    primary_model: str = os.getenv("PRIMARY_MODEL", "doubao-seed-2-0-lite-260428")
    fallback_model: str = os.getenv("FALLBACK_MODEL", "doubao-seed-2-1-pro-260628")

    # 向后兼容：`MODEL` 仍可整体覆盖主模型
    model: str = os.getenv("MODEL", os.getenv("PRIMARY_MODEL", "doubao-seed-2-0-lite-260428"))

    # 产品契约文档 第六节非功能约束：单次提问输入 token 上限 10 000。
    # 这是成本闸门，是引流产品的生死线——调整须回产品审核（产研边界约定 第 5 节）。
    max_input_tokens: int = int(os.getenv("MAX_INPUT_TOKENS", "15000"))

    # 语料版本号，答案页固定展示（验收标准文档 A1 验收标准第三条）
    corpus_version: str = os.getenv("CORPUS_VERSION", "ppwr-2026-09-05")

    # 深度思考默认**关**（该项裁决，2026-09-07 实测后落定）。
    #
    # 延迟的主导变量是思考不是模型：lite 默认 33.4s、压在 20 秒验收线之上，
    # 产品因此把关思考从可选优化改为**必做项**（产品契约文档 第六节响应时间双指标）。
    # 剩下的问题只有一个——质量代价。实测结论（scripts/d12_thinking.py）：
    #
    #   15 条黄金用例关思考跑一轮，机器判据（next_action／期望引用／位阶升序与
    #   3、4 必带无约束力标注／冲突呈现）**全过**；逐条读正文比对 `forbid`，
    #   挑出四条可疑的再跑一轮开思考对照——**三条开关表现相同，
    #   一条（某条用例）关思考的引用反而更全**（多引了第 6(3) 条）。**无一处退化由关思考造成。**
    #
    #   模型段耗时 25.5s → 7.8s，单问成本 ¥0.0073 → ¥0.0042。
    #
    # 出现存疑答案时 `ARK_THINKING=on` 一秒回滚，用来判断是不是思考的问题。
    thinking_off: bool = os.getenv("ARK_THINKING", "off").lower() in ("off", "0", "false")



settings = Settings()
