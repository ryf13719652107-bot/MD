import pytest
from app.services.martingale_engine import MartingaleEngine, MartingaleResult


def make_position(quantity: float, entry_price: float) -> dict:
    return {"quantity": quantity, "entry_price": entry_price}


def test_calculate_position_size():
    eng = MartingaleEngine(base_quantity=1.0, multiplier=2.0, max_layers=4)
    assert eng.calculate_position_size(0) == 1.0
    assert eng.calculate_position_size(1) == 2.0
    assert eng.calculate_position_size(2) == 4.0
    assert eng.calculate_position_size(3) == 8.0
    # Should cap at max_layers
    assert eng.calculate_position_size(5) == 8.0


def test_calculate_position_size_custom_multiplier():
    eng = MartingaleEngine(base_quantity=10.0, multiplier=1.5, max_layers=8)
    assert eng.calculate_position_size(0) == 10.0
    assert eng.calculate_position_size(1) == 15.0
    assert eng.calculate_position_size(2) == 22.5


def test_should_add_position_price_dropped():
    """Long position: price dropped 30% from entry should trigger add."""
    eng = MartingaleEngine(base_quantity=1.0, price_drop_pct=30.0)
    result = eng.should_add_position(current_layer=0, last_entry_price=100.0, current_price=70.0, side="long")
    assert result.should_add is True
    assert result.next_layer == 1


def test_should_add_position_price_not_dropped_enough():
    """Price only dropped 10% should not trigger."""
    eng = MartingaleEngine(base_quantity=1.0, price_drop_pct=30.0)
    result = eng.should_add_position(current_layer=0, last_entry_price=100.0, current_price=90.0, side="long")
    assert result.should_add is False


def test_should_add_position_max_layers():
    """At max layers, should not add more."""
    eng = MartingaleEngine(base_quantity=1.0, max_layers=3)
    result = eng.should_add_position(current_layer=3, last_entry_price=100.0, current_price=50.0, side="long")
    assert result.should_add is False


def test_check_take_profit_long():
    eng = MartingaleEngine(base_quantity=1.0, take_profit_pct=2.0)
    assert eng.check_take_profit(100.0, 103.0, "long") is True
    assert eng.check_take_profit(100.0, 101.0, "long") is False


def test_check_take_profit_short():
    eng = MartingaleEngine(base_quantity=1.0, take_profit_pct=2.0)
    assert eng.check_take_profit(100.0, 97.0, "short") is True
    assert eng.check_take_profit(100.0, 99.0, "short") is False


def test_calculate_take_profit_price():
    eng = MartingaleEngine(base_quantity=1.0, take_profit_pct=2.0)
    assert eng.get_take_profit_price(100.0, "long") == 102.0
    assert eng.get_take_profit_price(100.0, "short") == 98.0


def test_avg_entry_price():
    eng = MartingaleEngine(base_quantity=1.0)
    positions = [
        make_position(1.0, 100.0),
        make_position(2.0, 90.0),
    ]
    avg, total = eng.get_avg_entry_price(positions)
    assert total == 3.0
    assert abs(avg - (100.0 + 180.0) / 3.0) < 0.01
