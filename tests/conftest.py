"""Shared fixtures. Home Assistant fixtures load only when pytest-homeassistant-custom-component is installed."""

import importlib.util

import pytest

_HAS_HA = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None

pytest_plugins = ["pytest_homeassistant_custom_component"] if _HAS_HA else []

if _HAS_HA:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Let Home Assistant load the integration from custom_components."""
        return

else:

    @pytest.fixture
    def socket_enabled():
        """Stand in for the Home Assistant fixture that unblocks sockets."""
        return
