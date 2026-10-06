"""火山方舟延迟专项测试。

闸门（`gate_ark.py`）测的是 schema 能不能落地，结果是**能**；但顺带量出
pro 130–152 秒、lite 32–46 秒，而产品契约文档 第六节要的是「**首字节 3 秒内，用流式返回**」。

那两个数是**整个响应完成**的耗时，不是首字节——口径本身就对不上。
这里换成流式重测，另外看关掉深度思考能省多少。

**顺带校准 token 口径**：从响应的 `usage` 取真实用量，与 `estimate_tokens` 的
经验系数比对。该项裁决、该项裁决 的成本结论此前都标着「估算不是实测」，这一轮把它坐实或推翻。

    ~/.venvs/kernel/bin/python scripts/gate_ark_latency.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from app.answer import SYSTEM_PROMPT, Answer, build_messages  # noqa: E402
from app.config import ROOT, settings  # noqa: E402
from app.context import assemble, budget_report, estimate_tokens, fit_to_budget  # noqa: E402
from openai.lib._parsing._responses import type_to_text_format_param  # noqa: E402

from app.providers import ArkClient  # noqa: E402
from app.retrieval import retrieve  # noqa: E402

DEV_NOTES = ROOT / "DEV_NOTES"           # 实测明细落这里，与报告放一起

MODELS = ["doubao-seed-2-1-pro-260628", "doubao-seed-2-0-lite-260428"]
PRICING = {"doubao-seed-2-1-pro-260628": (6.0, 30.0),
           "doubao-seed-2-0-lite-260428": (0.6, 3.6)}

# 延迟与用哪条用例无关，只跑一条，省 token
CASE_Q = "包装里重金属最多能有多少？"

TTFT_TARGET = 3.0        # 产品契约文档 第六节：首字节 3 秒内


def measure(client, model, instructions, messages, thinking_off: bool) -> dict:
    """流式跑一次，量首字节与总耗时，并取真实 usage。"""
    kw = {}
    if thinking_off:
        # 豆包的深度思考开关。**不确定这个模型收不收**——收就用，不收就如实记下来，
        # 不去猜一个参数名硬塞。
        kw["extra_body"] = {"thinking": {"type": "disabled"}}

    # 不用 SDK 的 responses.stream 助手——它的累加器解析火山的事件流会崩
    # （'NoneType' object has no attribute 'append'）。改成 create(stream=True)
    # 自己数事件：schema 仍走官方转换，strict 保持 true。
    t0 = time.time()
    ttft = None
    u = None
    try:
        events = client._inner.responses.create(      # noqa: SLF001
            model=model,
            instructions=instructions,
            input=[{"role": m["role"], "content": m["content"]} for m in messages],
            max_output_tokens=16000,
            text={"format": type_to_text_format_param(Answer)},  # 要包一层 format
            stream=True,
            **kw,
        )
        for ev in events:
            if ttft is None:
                ttft = time.time() - t0
            u = getattr(getattr(ev, "response", None), "usage", None) or u
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:180]}"}

    total = time.time() - t0
    return {
        "ok": True,
        "ttft": round(ttft or total, 2),
        "total": round(total, 2),
        "in_tokens": getattr(u, "input_tokens", None),
        "out_tokens": getattr(u, "output_tokens", None),
    }


def main() -> int:
    client = ArkClient()
    with psycopg.connect(settings.database_url) as conn:
        r = retrieve(conn, CASE_Q)
        sys_tok = estimate_tokens(SYSTEM_PROMPT)
        ctx, _ = fit_to_budget(assemble(conn, r), reserved=sys_tok)
        messages = build_messages(CASE_Q, ctx)
    est_in = budget_report(ctx)["est_tokens"] + sys_tok

    print(f"用例「{CASE_Q}」· 上下文 {len(ctx)} 条 · 我的估算输入 {est_in} tok")
    print(f"目标：首字节 ≤ {TTFT_TARGET}s（产品契约文档 第六节）\n")
    print(f"{'模型':<30} {'配置':<12} {'首字节':>8} {'总耗时':>8} {'真实输入':>8} {'输出':>6}")
    print("─" * 80)

    out = []
    for model in MODELS:
        for thinking_off in (False, True):
            label = "关思考" if thinking_off else "默认"
            m = measure(client, model, SYSTEM_PROMPT, messages, thinking_off)
            m |= {"model": model, "config": label}
            out.append(m)
            if not m["ok"]:
                print(f"{model:<30} {label:<12} ✗ {m['err']}")
                continue
            print(f"{model:<30} {label:<12} {m['ttft']:>7.1f}s {m['total']:>7.1f}s "
                  f"{str(m['in_tokens']):>8} {str(m['out_tokens']):>6}"
                  + ("" if m["ttft"] <= TTFT_TARGET else "   ← 超标"))

    ok = [m for m in out if m["ok"] and m["in_tokens"]]
    if ok:
        real = ok[0]["in_tokens"]
        print(f"\n=== token 口径校准 ===")
        print(f"  我的估算 {est_in} · 真实 {real} · 偏差 {(est_in-real)/real*100:+.0f}%")
        print(f"  {'✓ 估算可用，该项裁决/该项裁决 的成本结论坐实' if abs(est_in-real)/real < 0.2 else '✗ 偏差过大，该项裁决/该项裁决 的闸门判断要按真实值重算'}")

        print(f"\n=== 单问成本（真实用量）===")
        for m in ok:
            pin, pout = PRICING[m["model"]]
            c = m["in_tokens"]*pin/1e6 + (m["out_tokens"] or 0)*pout/1e6
            print(f"  {m['model']:<30} {m['config']:<8} ¥{c:.3f}")

    (DEV_NOTES / "gate_ark_latency.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n明细已写入 DEV_NOTES/gate_ark_latency.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
