"""Reconnect backoff."""

from custom_components.comelit_vip.const import RECONNECT_BACKOFF, STABLE_CONNECTION


def test_backoff_grows():
    assert RECONNECT_BACKOFF[0] >= 5
    assert list(RECONNECT_BACKOFF) == sorted(RECONNECT_BACKOFF)
    assert RECONNECT_BACKOFF[-1] >= 300


def test_stable_window_outlasts_early_retries():
    assert RECONNECT_BACKOFF[1] < STABLE_CONNECTION


def test_probe_inside_idle_period():
    """An idle panel sends nothing between calls."""
    from custom_components.comelit_vip.const import KEEPALIVE_INTERVAL, WATCHDOG_INTERVAL

    assert WATCHDOG_INTERVAL <= KEEPALIVE_INTERVAL
    assert KEEPALIVE_INTERVAL <= 300
