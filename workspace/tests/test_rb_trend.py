from decimal import Decimal

from strategies.rb_trend import TrendInputs, TrendParameters, signal_ready


def test_signal_waits_until_every_condition_is_satisfied():
    inputs = TrendInputs(True, True, Decimal("0.926"))
    assert signal_ready(inputs) is False


def test_threshold_variant_can_be_evaluated_without_execution_access():
    inputs = TrendInputs(True, True, Decimal("0.926"))
    parameters = TrendParameters(volume_threshold=Decimal("0.90"))
    assert signal_ready(inputs, parameters) is True
