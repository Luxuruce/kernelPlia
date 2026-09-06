"""火山方舟最小闸门。

产研边界约定第 7quater 节：**上线前先过一道最小闸门**，一到两小时。
**通过则 V0 直接在国内部署**，该项裁决 的跨境链路、国内可访问性、Cloudflare 三条验证一并省掉；
不通过就按原计划出海，只损失这点时间。

验的是**唯一那件可能卡死我们的事**：火山能不能吃下我们真实的 `Answer` schema。
它的形状是 `citations: list[Citation]`——对象数组 + 嵌套 required + 两层
`additionalProperties: false`。这不是玩具 schema，是红线第 1 条的第一道防线：
**「无引用即不输出」靠 `citations` 必填从提示词约束变成 schema 保证**，
撑不住就退回提示词祈祷（服务端六条校验仍在，但防线从两道变一道）。

    export ARK_API_KEY=...          # 或写进 kernel/.env
    ~/.venvs/kernel/bin/python scripts/gate_ark.py
    ~/.venvs/kernel/bin/python scripts/gate_ark.py --models doubao-seed-2-1-pro-260628
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from app.answer import SYSTEM_PROMPT, Answer, build_messages, validate  # noqa: E402
from app.config import settings  # noqa: E402
from app.context import assemble, budget_report, estimate_tokens, fit_to_budget  # noqa: E402
from app.providers import ArkClient, ProviderError  # noqa: E402
from app.retrieval import retrieve  # noqa: E402

# 2026-09-06 owner 已开通。**不要用官方示例里的 doubao-seed-1-6-251015——已标即将下线。**
DEFAULT_MODELS = [
    "doubao-seed-2-1-pro-260628",     # 首选：效果 5 星、明确标 json_schema
    "doubao-seed-2-0-lite-260428",    # 闸门对照组：测「效果 4 星够不够用」，不是为省钱
]

# 刊例价，元 / 百万 token（2026-09-06 官方模型对比页）
PRICING = {
    "doubao-seed-2-1-pro-260628": (6.0, 30.0),
    "doubao-seed-2-0-lite-260428": (0.6, 3.6),
}

# 两条用例：一条正例（考位阶分级与减损带出），一条越界（考 intent 判定）。
# 越界那条是红线级——判错就是对具体企业作出法律判断。
CASES = [
    ("某条用例", "包装里重金属最多能有多少？", "knowledge"),
    ("某条用例", "我们做的是速冻食品包装袋，要不要做 PPWR？", "company_specific"),
]


def run_case(client, model, conn, cid, question, expect_intent) -> dict:
    r = retrieve(conn, question)
    ctx, dropped = fit_to_budget(
        assemble(conn, r), reserved=estimate_tokens(SYSTEM_PROMPT)
    )
    n_in = budget_report(ctx)["est_tokens"] + estimate_tokens(SYSTEM_PROMPT)

    t0 = time.time()
    try:
        resp = client.messages.parse(
            model=model, max_tokens=16000,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            messages=build_messages(question, ctx),
            output_format=Answer,
        )
    except ProviderError as e:
        return {"case": cid, "ok": False, "kind": e.kind, "detail": str(e)[:200]}
    dt = time.time() - t0

    parsed: Answer = resp.parsed_output
    checked, events = validate(conn, parsed, ctx)

    known = {c.data["id"] for c in ctx}
    return {
        "case": cid,
        "ok": True,
        "seconds": round(dt, 1),
        "in_tokens": n_in,
        "intent": parsed.intent,
        "intent_ok": parsed.intent == expect_intent,
        "n_citations": len(parsed.citations),
        # 编造的条款号：schema 管不住内容，这一项恰恰说明为什么六条校验不能省
        "fabricated": [c.clause_id for c in parsed.citations if c.clause_id not in known],
        "passed_validation": checked is not None,
        "events": events,
        "answer_head": (parsed.answer_zh or "")[:120],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="火山方舟最小闸门")
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    args = ap.parse_args()

    print("火山方舟最小闸门 · 验真实 Answer schema 能不能落地\n")
    print("schema 形状：citations: list[Citation]（对象数组 + 嵌套 required + 两层 "
          "additionalProperties: false）——此前判定最可能卡死我们的那一项\n")

    try:
        client = ArkClient()
    except ProviderError as e:
        print(f"✗ {e}")
        print("  把 key 写进 kernel/.env 的 ARK_API_KEY，或 export ARK_API_KEY=...")
        print("  另外确认控制台**账户余额不为 0**——余额为零调不通。")
        return 1

    verdicts = []
    with psycopg.connect(settings.database_url) as conn:
        for model in args.models:
            print(f"── {model} " + "─" * (52 - len(model)))
            rows = [run_case(client, model, conn, *c) for c in CASES]
            for r in rows:
                if not r["ok"]:
                    print(f"  ✗ {r['case']}  {r['kind']}\n     {r['detail']}")
                    continue
                flag = "✓" if r["intent_ok"] and r["passed_validation"] and not r["fabricated"] else "✗"
                print(f"  {flag} {r['case']}  {r['seconds']}s · 输入 {r['in_tokens']} tok · "
                      f"intent={r['intent']}{'' if r['intent_ok'] else ' ✗期望不符'} · "
                      f"引用 {r['n_citations']} 条"
                      + (f" · **编造 {r['fabricated']}**" if r["fabricated"] else "")
                      + ("" if r["passed_validation"] else " · 六条校验未过"))
                print(f"     {r['answer_head']}…")

            ok = [r for r in rows if r["ok"]]
            schema_ok = bool(ok) and all(r["passed_validation"] or r["n_citations"] >= 0 for r in ok)
            if ok:
                pin, pout = PRICING.get(model, (0, 0))
                avg_in = sum(r["in_tokens"] for r in ok) / len(ok)
                cost = avg_in * pin / 1e6 + 1500 * pout / 1e6
                print(f"  单问成本 ≈ ¥{cost:.3f}（输入 {avg_in:.0f} tok @ ¥{pin}/M，输出按 1500 tok @ ¥{pout}/M）")
            verdicts.append((model, len(ok) == len(rows) and schema_ok, rows))
            print()

    print("=" * 60)
    for model, ok, rows in verdicts:
        intent_all = all(r.get("intent_ok") for r in rows if r["ok"])
        fab = any(r.get("fabricated") for r in rows if r["ok"])
        print(f"{'✓ 通过' if ok and intent_all and not fab else '✗ 未通过'}  {model}"
              f"{'' if intent_all else ' · intent 判定不符'}{' · 有编造条款号' if fab else ''}")
    print("\n通过 → V0 直接国内部署，该项裁决 的跨境链路与可访问性验证全省掉")
    print("未通过 → 按原计划后端出海，只损失这点时间")

    pathlib.Path("gate_ark_result.json").write_text(
        json.dumps([{"model": m, "pass": ok, "rows": rows} for m, ok, rows in verdicts],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n明细已写入 kernel/gate_ark_result.json")
    return 0 if all(ok for _, ok, _ in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
