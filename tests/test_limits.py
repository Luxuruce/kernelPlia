"""限流与预算。两级都**不硬停**——降级态比没上线更伤，但降级不等于白屏。"""

from __future__ import annotations

from app import limits


def test_按金额计不按token计():
    """原定「每月 100 万 token」是按某个模型的成本直觉定的，换个供应商就与真实成本脱钩：
    同样 100 万 token 在不同模型上差一个量级。所以预算的单位必须是钱。"""
    cheap = limits.cost_cny("doubao-seed-2-0-lite-260428", 1_000_000, 0)
    dear = limits.cost_cny("doubao-seed-2-1-pro-260628", 1_000_000, 0)
    assert cheap < dear


def test_没登记的模型按最贵算():
    """估错方向要偏保守——宁可早降级，不可超支。"""
    assert limits.cost_cny("未知模型", 1_000_000, 0) == \
        limits.cost_cny("doubao-seed-2-1-pro-260628", 1_000_000, 0)


def test_计数只在真的调了模型时增加(conn):
    """被闸门拦下的那一轮不计——否则拦一次也扣一次额度，越拦越拦得死。"""
    ip = "198.51.100.250"
    conn.execute("delete from rate_limit where ip = %s", (ip,))
    before = limits.check(conn, ip)
    assert before.allowed and before.used_today == 0
    limits.consume(conn, ip)
    assert limits.check(conn, ip).used_today == 1
    conn.execute("delete from rate_limit where ip = %s", (ip,))
    conn.commit()
