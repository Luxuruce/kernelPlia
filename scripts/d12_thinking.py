"""该项裁决：关思考的质量代价实测。

产品裁决（产品契约文档 第六节「关思考的质量代价怎么判」）：

> 批准花约 ¥2 跑一轮 15 条用例。**判据不是输出长度**——输出从 1 480 token
> 掉到 611 未必是变差，我们的答案格式是「一般规定 + 引用 + 位阶标注」，不是论文。
> 判据是逐条比对 `expect_behavior` 与 `forbid`：**位阶分级对不对、三段呈现有没有、
> 有没有越界、引用有没有丢**。只有这四项出现退化才算有质量代价。

所以这个脚本做两件事，不做第三件：

1. **机器能判的四项照单全查**——`next_action`、期望引用是否到齐、位阶是否升序且
   3/4 带标注、拦截层是否照常触发；
2. **答案正文原样落盘**，供人对着 `expect_behavior` 与 `forbid` 读；
3. **不打分**。`forbid` 是自然语言判断，脚本假装能判会把「看起来通过」当成通过。

    ARK_THINKING=off ~/.venvs/kernel/bin/python scripts/d12_thinking.py
    ~/.venvs/kernel/bin/python scripts/d12_thinking.py --dry-run   # 先看要花多少

**会真的花钱。** 火山余额有限（见《待决项清单》待办），先跑 --dry-run。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml

from app.answer import ask
from app.config import KNOWLEDGE_DIR, settings
from app.db import connect
from app.limits import cost_cny
from app.providers import make_client

OUT = pathlib.Path(__file__).resolve().parent.parent / "DEV_NOTES" / "d12_thinking.json"


def load_cases() -> list[dict]:
    doc = yaml.safe_load((KNOWLEDGE_DIR / "golden_cases.yaml").read_text(encoding="utf-8"))
    return doc["cases"]


def check(case: dict, res) -> dict:
    """机器能判的那几项。**判不了的一律不判**，留给人读正文。"""
    cited = [c["clause_id"] for c in res.citations]
    ranks = [c["authority_rank"] for c in res.citations]
    notes = [c.get("authority_note") for c in res.citations]
    expect = case.get("expect_clauses") or []
    return {
        # 一、越界与阻断：next_action 与用例期望是否一致
        "next_action_ok": res.next_action == case.get("expect_next_action", "none"),
        "next_action": res.next_action,
        "intent": res.intent,
        # 二、引用有没有丢
        "missing_expected": [c for c in expect if c not in cited],
        "cited": cited,
        # 三、位阶分级：升序，且 3/4 必带无约束力标注（红线第 10 条）
        "rank_sorted": ranks == sorted(ranks),
        "rank_note_ok": all(
            (r < 3) or (n and "无法律约束力" in n) for r, n in zip(ranks, notes)
        ),
        # 四、冲突是否照常呈现
        "conflicts": [c["conflict_id"] for c in res.conflicts],
        "expect_conflicts": case.get("expect_conflict_ids") or [],
        "events": res.events,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只估算调用次数与花费，不调模型")
    a = ap.parse_args()

    cases = load_cases()
    print(f"思考：{'关' if settings.thinking_off else '开（默认）'}　模型：{settings.primary_model}")

    if a.dry_run:
        # 阻断轮不调模型（产品契约文档 第三节冲突阻断流程），先数清楚要花几次
        n = sum(1 for c in cases if not (c.get("expect_conflict_ids") or []))
        est = cost_cny(settings.primary_model, 9_000, 1_000) * n
        print(f"用例 {len(cases)} 条 → **实际调模型 {n} 次**"
              f"（{len(cases) - n} 条第一轮命中冲突、不调模型）")
        print(f"按每次 9k 输入 / 1k 输出估：约 ¥{est:.3f}，约 {n * 10_000 // 1000}k token")
        return

    client = make_client()
    rows = []
    with connect() as conn:
        for c in cases:
            t0 = time.time()
            res = ask(conn, c["q"], client=client)
            ms = int((time.time() - t0) * 1000)
            u = res.usage or {}
            rows.append({
                "id": c["id"], "q": c["q"], "ms": ms,
                "model": res.model_used, "usage": u,
                "cny": round(cost_cny(res.model_used, u.get("in", 0), u.get("out", 0)), 5)
                       if res.model_used else 0,
                "answer_zh": res.answer_zh,
                "expect_behavior": c.get("expect_behavior", ""),
                "forbid": c.get("forbid", []),
                **check(c, res),
            })
            bad = [k for k in ("next_action_ok", "rank_sorted", "rank_note_ok") if not rows[-1][k]]
            flag = "✗ " + ",".join(bad) if bad else ("△ 缺引用" if rows[-1]["missing_expected"] else "✓")
            print(f"  {c['id']:<4} {ms:>6} ms  出 {u.get('out', 0):>5} tok  {flag}")

    total_ms = sum(r["ms"] for r in rows)
    called = [r for r in rows if r["model"]]
    print(f"\n调模型 {len(called)}/{len(rows)} 次 · 合计 ¥{sum(r['cny'] for r in rows):.4f}"
          f" · 均耗时 {total_ms // max(len(called), 1)} ms")
    OUT.write_text(json.dumps(
        {"thinking_off": settings.thinking_off, "model": settings.primary_model, "cases": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"正文与逐条明细 → {OUT}")


if __name__ == "__main__":
    main()
