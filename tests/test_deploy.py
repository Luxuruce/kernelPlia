"""部署闸门。**都是构建期发现不了、上线才炸的**，所以放在每次都跑的回归里。

原先在 `kernel/deploy/sync.sh` 里，2026-10-06 仓库迁移后不再同步，挪到这里（不依赖库）。
两条都踩过：

- 2026-09-08 `pyproject.toml` 漏了依赖表，Vercel 一个包都没装；
- 同日 `DAILY_LIMIT_PER_IP` 在 Vercel 上是空字符串，`int('')` 抛在模块级，线上所有路由 500。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 只在本地用的包：起服务、测试、重渲页图、备用供应商。不进函数包。
DEV_ONLY = {"uvicorn", "pytest", "httpx", "pypdfium2", "pillow", "anthropic"}


def _name(spec: str) -> str:
    return re.split(r"[\[<>=~!;\s]", spec.strip(), maxsplit=1)[0].lower()


def test_requirements里的运行时依赖都在pyproject里():
    """**Vercel 只读 pyproject.toml**——漏了不会报错，只会在线上模块级 ImportError，所有路由 500。"""
    req = {_name(l) for l in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.lstrip().startswith("#")}
    proj = {_name(d) for d in tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]}
    missing = sorted(req - DEV_ONLY - proj)
    assert not missing, f"requirements.txt 有、pyproject.toml 没有：{missing}"


def test_环境变量全部置空也能import():
    """模拟托管平台「键在、值空」：Vercel 从 .env.example 认出键、值留空建好。"""
    env = dict(os.environ)
    for k in ("DAILY_LIMIT_PER_IP", "MONTHLY_BUDGET_CNY", "MAX_INPUT_TOKENS", "DATABASE_URL",
              "PRIMARY_MODEL", "FALLBACK_MODEL", "ARK_THINKING", "LLM_PROVIDER",
              "PAGE_IMAGE_DIR", "PAGE_IMAGE_URL_PREFIX", "CORPUS_ROOT"):
        env[k] = ""
    r = subprocess.run([sys.executable, "-c", "import app.api"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_密钥文件没有入库():
    if not (ROOT / ".git").exists():
        pytest.skip("不是 git 工作区")
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.splitlines()
    bad = [f for f in files if re.search(r"(^|/)\.env$|\.pem$|github_key", f)]
    assert not bad, f"密钥文件进了版本库：{bad}"
