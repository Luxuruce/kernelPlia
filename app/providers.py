"""模型供应商适配。

`synthesize()` 只认一个接口——`client.messages.parse(...)`。本模块让火山方舟
也长成那个样子，**`app/answer.py` 一行不用改**。

为什么是薄的：火山兼容 OpenAI SDK，而 OpenAI 的 `responses.parse(text_format=<Pydantic>)`
与 Anthropic 的 `messages.parse(output_format=<Pydantic>)` 几乎同构——
Pydantic 模型直接传，两边都不用自己导出 JSON Schema。

供应商由 `LLM_PROVIDER` 选：`anthropic`（默认）| `ark`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.config import env, settings

# 火山方舟 Responses API。端点与鉴权见控制台「快速开始」。
ARK_BASE_URL = env("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")


class ProviderError(Exception):
    """供应商侧的失败。分层见 `classify` ——不同类别要走不同降级。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def classify(exc: Exception) -> str:
    """把异常分层。

    产品契约文档 第六节要求「模型调用失败返回固定文案」，但**四类失败要走不同的路**：
    额度耗尽要走超限降级、限流要重试、模型拒答要如实说、代码错要暴露出来修。
    产品侧查出的缺口正是这里——`except Exception` 把它们压成一种，
    **超限降级永远触发不了**。
    """
    name = type(exc).__name__
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)

    if status == 429 or "rate limit" in text or "tpm" in text or "rpm" in text:
        return "rate_limited"
    if status in (402, 403) or "insufficient" in text or "balance" in text or "quota" in text:
        return "quota_exhausted"        # → 超限降级：只出两段式第一段
    if status == 400 and ("schema" in text or "format" in text):
        return "schema_rejected"        # → 结构化输出不被支持，闸门要红
    if status and 500 <= int(status) < 600:
        return "upstream_error"
    if isinstance(exc, (TypeError, AttributeError, KeyError, NameError)):
        return "bug"                    # → 我们自己的代码错，不该被当成模型故障吞掉
    return "unknown"


@dataclass
class _Parsed:
    parsed_output: Any
    #: 实际用量。月度预算改按**金额**计（该项裁决），没有这个就只能拿估算记账。
    usage: dict | None = None


class _ArkMessages:
    """把 Anthropic 的 `messages.parse` 形状翻译成火山的 `responses.parse`。"""

    def __init__(self, inner):
        self._inner = inner

    def parse(self, *, model, max_tokens, system, messages, output_format, **_):
        # 该项裁决：关思考。火山把它放在 `extra_body`，不是标准 Responses 参数。
        extra = {"thinking": {"type": "disabled"}} if settings.thinking_off else None
        # system 在我们这边是带 cache_control 的块列表；火山走 instructions 参数。
        # 这一层同时也是 该项裁决「稳定指令层」的落点——instructions 逐请求不变。
        instructions = "\n\n".join(
            b["text"] if isinstance(b, dict) else str(b) for b in system
        )
        try:
            resp = self._inner.responses.parse(
                model=model,
                instructions=instructions,
                input=[{"role": m["role"], "content": m["content"]} for m in messages],
                max_output_tokens=max_tokens,
                text_format=output_format,
                **({"extra_body": extra} if extra else {}),
            )
        except Exception as e:                      # noqa: BLE001 —— 立刻分层，不吞
            raise ProviderError(classify(e), str(e)[:300]) from e

        parsed = getattr(resp, "output_parsed", None)
        if parsed is None:
            raise ProviderError("schema_rejected", "返回里没有 output_parsed")
        u = getattr(resp, "usage", None)
        return _Parsed(
            parsed_output=parsed,
            usage={
                "in": getattr(u, "input_tokens", 0) or 0,
                "out": getattr(u, "output_tokens", 0) or 0,
            } if u else None,
        )


class ArkClient:
    """火山方舟客户端，对外长得像 `anthropic.Anthropic`。"""

    def __init__(self, api_key: str | None = None, base_url: str = ARK_BASE_URL):
        import openai

        key = api_key or env("ARK_API_KEY")
        if not key:
            raise ProviderError("no_credentials", "未设置 ARK_API_KEY")
        self._inner = openai.OpenAI(api_key=key, base_url=base_url)
        self.messages = _ArkMessages(self._inner)


def make_client(provider: str | None = None):
    """按 `LLM_PROVIDER` 造客户端。默认 anthropic，与技术栈定稿一致。"""
    provider = (provider or env("LLM_PROVIDER", "anthropic")).lower()

    if provider == "ark":
        return ArkClient()

    if provider == "anthropic":
        import anthropic

        if not settings.anthropic_api_key:
            raise ProviderError("no_credentials", "未设置 ANTHROPIC_API_KEY")
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)

    raise ProviderError("unknown_provider", provider)
