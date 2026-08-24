"""Configuration parsing."""

import pytest

from custom_components.comelit_vip.viper.models import PanelConfig

RESPONSE = {
    "viper-client": {"description": "iPhone"},
    "vip": {
        "apt-address": "SB000042",
        "apt-subaddress": 1,
        "user-parameters": {
            "entrance-address-book": [{"id": 0, "name": "Entrance", "apt-address": "SB900001"}],
            "opendoor-address-book": [
                {"id": 0, "name": "Entrance lock", "apt-address": "SB900001", "output-index": 1, "secure-mode": False}
            ],
            "actuator-address-book": [
                {"id": 0, "name": "Actuator", "apt-address": "SBIO0999", "module-index": 255, "output-index": 1}
            ],
        },
    },
}


def test_parse_config():
    config = PanelConfig.from_response(RESPONSE)
    assert config.source == "SB0000421"
    assert config.entrance == "SB900001"
    assert [d.name for d in config.doors] == ["Entrance lock"]
    assert config.doors[0].output_index == 1
    assert [a.address for a in config.actuators] == ["SBIO0999"]
    assert config.description == "iPhone"


def test_entries_without_address_are_skipped():
    payload = {
        "vip": {
            "apt-address": "SB000042",
            "apt-subaddress": 1,
            "user-parameters": {"opendoor-address-book": [{"id": 0, "name": "Broken"}]},
        }
    }
    assert PanelConfig.from_response(payload).doors == []


def test_keys_unique():
    config = PanelConfig.from_response(RESPONSE)
    keys = [d.key for d in config.doors] + [a.key for a in config.actuators]
    assert len(set(keys)) == len(keys)


def test_missing_apt_address_rejected():
    from custom_components.comelit_vip.viper.errors import ViperError

    with pytest.raises(ViperError):
        PanelConfig.from_response({"vip": None})


def test_null_number_defaults():
    config = PanelConfig.from_response({"vip": {"apt-address": "SB000042", "apt-subaddress": None}})

    assert config.apt_subaddress == 1
    assert config.source == "SB0000421"


def test_null_table_defaults():
    config = PanelConfig.from_response({"vip": {"apt-address": "SB000042", "apt-subaddress": 2, "user-parameters": None}})

    assert config.entrances == []
    assert config.doors == []
    assert config.entrance is None


def test_non_dict_row_skipped():
    config = PanelConfig.from_response(
        {
            "vip": {
                "apt-address": "SB000042",
                "apt-subaddress": 2,
                "user-parameters": {
                    "entrance-address-book": ["nonsense", {"name": "Entrance", "apt-address": "SB900001"}],
                    "opendoor-address-book": None,
                },
            }
        }
    )

    assert config.entrances == [("Entrance", "SB900001")]
    assert config.doors == []


def test_unencodable_addresses_skipped():
    response = {
        "vip": {
            "apt-address": "SB000042",
            "user-parameters": {
                "opendoor-address-book": [
                    {"id": 0, "name": "Long", "apt-address": "SB0000000001"},
                    {"id": 1, "name": "Fine", "apt-address": "SB900001"},
                ],
            },
        }
    }
    config = PanelConfig.from_response(response)
    assert [d.name for d in config.doors] == ["Fine"]


def test_output_index_must_fit_a_byte():
    response = {
        "vip": {
            "apt-address": "SB000042",
            "user-parameters": {
                "opendoor-address-book": [{"id": 0, "name": "Door", "apt-address": "SB900001", "output-index": 256}],
            },
        }
    }
    assert PanelConfig.from_response(response).doors[0].output_index == 1
