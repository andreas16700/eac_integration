"""Runtime tests against a real Home Assistant instance.

Requires pytest-homeassistant-custom-component. Run with:
    uv run --with pytest-homeassistant-custom-component python -m pytest tests/test_config_flow.py -q

Every test uses ``recorder_mock`` (the integration depends on recorder, so even
loading the config flow needs it) and unloads the entry to avoid the coordinator's
refresh timer lingering past the test.
"""

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eac.const import (
    CONF_CONSUMPTION,
    CONF_MONTH_RATES,
    CONF_PERIODS,
    CONF_TARIFF,
    DOMAIN,
)


def _entry(**options) -> MockConfigEntry:
    base = {CONF_PERIODS: [], CONF_TARIFF: {}, CONF_MONTH_RATES: {}}
    base.update(options)
    return MockConfigEntry(
        domain=DOMAIN, data={CONF_CONSUMPTION: "sensor.grid_import"}, options=base
    )


async def _unload(hass: HomeAssistant, entry_id: str) -> None:
    await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_user_config_flow(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONSUMPTION: "sensor.grid_import"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_CONSUMPTION] == "sensor.grid_import"
    assert entry.options[CONF_PERIODS] == []
    await hass.async_block_till_done()
    await _unload(hass, entry.entry_id)


async def test_setup_creates_sensors(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    entry = _entry(
        **{CONF_PERIODS: [{"id": "p1", "name": "Jan-Mar 2026", "start": "2026-01-20", "end": "2026-03-12"}]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    eac = [s.entity_id for s in hass.states.async_all() if s.entity_id.startswith("sensor.eac")]
    assert any("total" in e for e in eac), eac
    assert any("gross" in e for e in eac), eac
    await _unload(hass, entry.entry_id)


async def test_options_add_period(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_period"}
    )
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Jan-Mar 2026", "start": "2026-01-20", "end": "2026-03-12"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.options[CONF_PERIODS]) == 1
    assert entry.options[CONF_PERIODS][0]["name"] == "Jan-Mar 2026"
    # DateSelector must yield JSON-serialisable strings
    assert entry.options[CONF_PERIODS][0]["start"] == "2026-01-20"
    await _unload(hass, entry.entry_id)


async def test_options_month_rates_and_tariff(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """Regression: NumberSelector(step='any') in the month-rates and tariff steps."""
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "month_rates"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"month": "2026-03", "fuel_c": 8.0159, "production": 0.1789}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_MONTH_RATES]["2026-03"]["fuel_c"] == 8.0159
    assert entry.options[CONF_MONTH_RATES]["2026-03"]["production"] == 0.1789

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tariff"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"network": 0.03}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_TARIFF]["network"] == 0.03
    await _unload(hass, entry.entry_id)


async def test_reconfigure_changes_meter(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONSUMPTION: "sensor.grid_energy"}
    )
    assert result["type"] == FlowResultType.ABORT  # update_reload_and_abort
    await hass.async_block_till_done()
    assert entry.data[CONF_CONSUMPTION] == "sensor.grid_energy"
    await _unload(hass, entry.entry_id)


async def test_manual_override_period(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """A period with manual gross/export computes a bill without any statistics."""
    entry = _entry(**{CONF_PERIODS: [{
        "id": "p1", "name": "Jan-Mar 2026", "start": "2026-01-20", "end": "2026-03-12",
        "manual_gross_kwh": 732.05, "manual_export_kwh": 56.01,
    }]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    total = hass.states.get("sensor.eac_jan_mar_2026_total")
    assert total is not None and total.state not in ("unavailable", "unknown"), total
    a = total.attributes
    assert a["gross_imported_kwh"] == 732.05
    assert abs(a["net_imported_kwh"] - 676.04) < 0.001
    assert a["coverage_complete"] is True
    await _unload(hass, entry.entry_id)


async def test_current_period(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """A current period is auto-added, starting at the latest configured end."""
    entry = _entry(**{CONF_PERIODS: [
        {"id": "p1", "name": "P1", "start": "2025-11-20", "end": "2026-01-12"},
        {"id": "p2", "name": "P2", "start": "2026-01-20", "end": "2026-03-12"},
    ]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coord = hass.data[DOMAIN][entry.entry_id]
    current = coord._current_period()
    assert current is not None
    assert current["start"] == "2026-03-12"  # latest configured end
    assert current["end"] == dt_util.now().date().isoformat()  # today
    # its sensors exist
    assert hass.states.get("sensor.eac_current_total") is not None
    await _unload(hass, entry.entry_id)


async def test_no_current_period_without_periods(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    entry = _entry()  # no configured periods
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coord = hass.data[DOMAIN][entry.entry_id]
    assert coord._current_period() is None
    assert coord.all_periods() == []
    assert hass.states.get("sensor.eac_current_total") is None
    await _unload(hass, entry.entry_id)
