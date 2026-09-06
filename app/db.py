"""数据库连接。

单个 PostgreSQL，不上连接池组件——产品契约文档 第一节的贯穿原则是「能少一个组件就少一个」，
V0 的预期量级也不构成瓶颈（第六节：并发不做特别设计，过早优化浪费工期）。
"""

from __future__ import annotations

import psycopg

from app.config import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)
