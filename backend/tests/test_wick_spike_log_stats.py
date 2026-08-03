"""接针日志解析/统计单元测试。"""

from app.services.wick_spike_log_stats import (
    analyze_paths,
    format_report,
    parse_line,
)


def test_parse_near_miss_legacy_and_new():
    line = (
        "2026-08-03 09:22:10 [INFO] app.services.wick_spike_runner: "
        "wick_spike near-miss strategy=9 BLESSUSDT dir=short "
        "px=0.0221 open=0.021159 ext=0.0225 thr=0.02196 pierce=True "
        "atrN=0.0008 progress=1.66 amp%=6.34 vol×=4.90 need×=8 vol_hot=False"
    )
    row = parse_line(line)
    assert row is not None
    assert row.symbol == "BLESSUSDT"
    assert row.pierce is True
    assert abs(row.progress - 1.66) < 1e-9
    assert abs(row.vol_x - 4.9) < 1e-9
    assert row.vol_hot is False
    assert row.tip_gap_pct > 0


def test_parse_trigger_with_tip_gap():
    line = (
        "2026-08-03 10:00:00 [INFO] app.services.wick_spike_runner: "
        "wick_spike trigger strategy=10 AAAUSDT long px=1.0 open=1.05 ext=0.98 "
        "atrN=0.02 progress=3.50 tip_gap%=1.905 vol×=5.20 need×=5 "
        "trade_age_ms=12 detect_to_lock_ms=0.5"
    )
    row = parse_line(line)
    assert row.kind == "trigger"
    assert abs(row.tip_gap_pct - 1.905) < 1e-9
    assert abs(row.progress - 3.5) < 1e-9
    assert abs(row.detect_to_lock_ms - 0.5) < 1e-9
    assert row.trade_age_ms == 12


def test_speed_stats_in_analysis(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                "2026-08-03 10:00:00 [INFO] x: wick_spike trigger strategy=10 AAAUSDT long "
                "px=1.0 open=1.05 ext=0.98 atrN=0.02 progress=3.50 tip_gap%=1.9 "
                "vol×=5.20 need×=5 trade_age_ms=20 detect_to_lock_ms=1.5",
                "2026-08-03 10:00:01 [INFO] x: wick_spike opened strategy=10 AAAUSDT "
                "open_api_ms=85 trade_age_ms=25 px=1.0 open=1.05 ext=0.98 "
                "progress=3.50 tip_gap%=1.9 vol×=5.20 need×=5",
            ]
        ),
        encoding="utf-8",
    )
    from app.services.wick_spike_log_stats import build_analysis

    report = analyze_paths([log])
    data = build_analysis(report)
    assert data["detect_to_lock_ms"]["n"] == 1
    assert data["open_api_ms"]["n"] == 1
    assert data["open_api_ms"]["mean"] == 85
    assert data["trade_age_ms"]["n"] == 2
    assert "成交推送年龄" in data["text"]
    assert "下单:" in data["text"]
    assert "下单+写库" not in data["text"]
    assert "近失卡点" in data["text"] or "近失" in data["text"]


def test_parse_opened_legacy_open_api_db_ms():
    line = (
        "2026-08-03 10:00:01 [INFO] x: wick_spike opened strategy=10 AAAUSDT "
        "open_api_db_ms=120 trade_age_ms=25 px=1.0 open=1.05 ext=0.98 "
        "progress=3.50 tip_gap%=1.9 vol×=5.20 need×=5"
    )
    row = parse_line(line)
    assert row.kind == "opened"
    assert abs(row.open_api_ms - 120) < 1e-9


def test_vol_blocked_deep_filter(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                # 深针量不够 → 应收录
                "2026-08-03 09:22:10 [INFO] x: wick_spike near-miss strategy=9 BLESSUSDT "
                "dir=short px=0.022 open=0.021 ext=0.0225 thr=0.0215 pierce=True "
                "atrN=0.001 progress=1.66 vol×=4.90 need×=8 vol_hot=False",
                # progress 不够
                "2026-08-03 09:23:10 [INFO] x: wick_spike near-miss strategy=9 BLESSUSDT "
                "dir=short px=0.0212 open=0.021 ext=0.0213 thr=0.0215 pierce=False "
                "atrN=0.001 progress=0.30 vol×=5.00 need×=8 vol_hot=False",
                # 已开仓贴尖
                "2026-08-03 09:24:00 [INFO] x: wick_spike opened strategy=9 BLESSUSDT "
                "open_api_db_ms=80 trade_age_ms=15 px=0.0224 open=0.021 ext=0.0225 "
                "progress=1.50 tip_gap%=0.476 vol×=5.10 need×=5",
            ]
        ),
        encoding="utf-8",
    )
    report = analyze_paths([log])
    deep = report.vol_blocked_deep(progress_min=1.5)
    assert len(deep) == 1
    assert deep[0].vol_x == 4.9
    text = format_report(report, progress_min=1.5)
    assert "已刺破但量能不够" in text
    assert "进场距针尖" in text or "距针尖" in text
    assert "若量能门槛改为" in text or "反事实" in text or "可过" in text


def test_filter_report_by_strategies(tmp_path):
    from app.services.wick_spike_log_stats import filter_report_by_strategies

    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                "2026-08-03 09:22:10 [INFO] x: wick_spike near-miss strategy=9 AAAUSDT "
                "dir=short px=0.022 open=0.021 ext=0.0225 thr=0.0215 pierce=True "
                "atrN=0.001 progress=1.66 vol×=4.90 need×=8 vol_hot=False",
                "2026-08-03 09:22:11 [INFO] x: wick_spike near-miss strategy=12 BBBUSDT "
                "dir=long px=1 open=1.01 ext=0.99 thr=1.0 pierce=True "
                "atrN=0.01 progress=2.0 vol×=3.0 need×=8 vol_hot=False",
            ]
        ),
        encoding="utf-8",
    )
    report = analyze_paths([log])
    filtered = filter_report_by_strategies(report, {9})
    assert len(filtered.near_misses) == 1
    assert filtered.near_misses[0].strategy_id == 9


def test_clear_wick_spike_lines_by_strategy(tmp_path):
    from app.services.wick_spike_log_stats import clear_wick_spike_lines

    log = tmp_path / "bot.log"
    log.write_text(
        "\n".join(
            [
                "keep me unrelated",
                "2026-08-03 09:22:10 [INFO] x: wick_spike opened strategy=9 AAAUSDT "
                "open_api_db_ms=80 trade_age_ms=15 px=1 open=1 ext=1.1 progress=1 tip_gap%=0.1 vol×=5 need×=5",
                "2026-08-03 09:22:11 [INFO] x: wick_spike opened strategy=90 BBBUSDT "
                "open_api_db_ms=80 trade_age_ms=15 px=1 open=1 ext=1.1 progress=1 tip_gap%=0.1 vol×=5 need×=5",
                "2026-08-03 09:22:12 [INFO] x: wick_spike opened strategy=12 CCCUSDT "
                "open_api_db_ms=80 trade_age_ms=15 px=1 open=1 ext=1.1 progress=1 tip_gap%=0.1 vol×=5 need×=5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = clear_wick_spike_lines([log], strategy_ids={9, 12})
    assert result["lines_removed"] == 2
    text = log.read_text(encoding="utf-8")
    assert "strategy=9 " not in text
    assert "strategy=12 " not in text
    assert "strategy=90 " in text
    assert "keep me unrelated" in text

