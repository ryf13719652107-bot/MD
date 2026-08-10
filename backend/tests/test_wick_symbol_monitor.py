from app.services.wick_spike_log_stats import (
    EntryRow,
    NearMissRow,
    ReboundRow,
    SkipRow,
    WickLogReport,
    build_symbol_monitor,
)


def test_build_symbol_monitor_explains_depth_and_volume():
    report = WickLogReport(
        near_misses=[
            NearMissRow(
                ts="2026-08-04 02:58:10",
                strategy_id=9,
                symbol="BLESSUSDT",
                direction="long",
                px=0.016,
                open=0.0165,
                ext=0.0158,
                thr=0.0152,
                pierce=False,
                atr_n=0.0012,
                progress=0.6,
                vol_x=7.3,
                need_x=5.0,
                vol_hot=True,
            ),
            NearMissRow(
                ts="2026-08-04 02:32:05",
                strategy_id=9,
                symbol="BLESSUSDT",
                direction="long",
                px=0.017,
                open=0.018,
                ext=0.0165,
                thr=0.0168,
                pierce=True,
                atr_n=0.001,
                progress=1.2,
                vol_x=3.0,
                need_x=5.0,
                vol_hot=False,
            ),
        ],
        entries=[
            EntryRow(
                kind="opened",
                ts="2026-08-04 01:10:00",
                strategy_id=9,
                symbol="BLESSUSDT",
                side="long",
                progress=1.1,
                vol_x=6.0,
                need_x=5.0,
            )
        ],
    )
    out = build_symbol_monitor(report, "bless", list_limit=10)
    assert out["near_miss_n"] == 2
    assert out["opened_n"] == 1
    assert out["total"] == 3
    reasons = " ".join(r["reason"] for r in out["rows"])
    assert "深度不够" in reasons or "progress" in reasons
    assert "量能不足" in reasons


def test_build_symbol_monitor_empty_note():
    out = build_symbol_monitor(WickLogReport(), "PTBUSDT")
    assert out["total"] == 0
    assert "没有" in out["note"] or "无" in out["note"]


def test_build_symbol_monitor_includes_skip_and_rebound():
    report = WickLogReport(
        skips=[
            SkipRow(
                ts="2026-08-11 01:00:41",
                strategy_id=13,
                symbol="GRVTUSDT",
                reason="busy",
                detail="leg_lock_timeout",
            )
        ],
        rebounds=[
            ReboundRow(
                ts="2026-08-11 01:00:40",
                strategy_id=13,
                symbol="GRVTUSDT",
                event="rebound_fire",
            )
        ],
    )
    out = build_symbol_monitor(report, "GRVT", list_limit=10)
    assert out["skip_n"] == 1
    assert out["rebound_n"] == 1
    assert out["total"] == 2
    kinds = {r["kind"] for r in out["rows"]}
    assert "skip" in kinds and "rebound" in kinds
    reasons = " ".join(r["reason"] for r in out["rows"])
    assert "腿锁" in reasons or "busy" in reasons
    assert "开火" in reasons
