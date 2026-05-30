"""Config and options flow for the EAC integration."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .billing import Tariff
from .const import (
    CONF_CONSUMPTION,
    CONF_EXPORT,
    CONF_MONTH_RATES,
    CONF_PERIODS,
    CONF_TARIFF,
    DEFAULT_NAME,
    DOMAIN,
    M_FUEL_C,
    M_PRODUCTION,
    P_END,
    P_ID,
    P_MANUAL_EXPORT,
    P_MANUAL_GROSS,
    P_NAME,
    P_RATE_MONTH,
    P_START,
    TARIFF_FIELDS,
)
from .rates import bundled_fuel_rate

_ENERGY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
)
_DATE_SELECTOR = selector.DateSelector()
_TEXT_SELECTOR = selector.TextSelector()


def _number() -> selector.NumberSelector:
    # step="any" allows the precision EAC rates need (e.g. 0.00051, 0.1789);
    # numeric steps below 0.001 are rejected by the selector schema.
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, step="any", mode=selector.NumberSelectorMode.BOX)
    )


class EacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: choose the meters."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {CONF_CONSUMPTION: user_input[CONF_CONSUMPTION]}
            if user_input.get(CONF_EXPORT):
                data[CONF_EXPORT] = user_input[CONF_EXPORT]
            return self.async_create_entry(
                title=DEFAULT_NAME,
                data=data,
                options={CONF_PERIODS: [], CONF_TARIFF: {}, CONF_MONTH_RATES: {}},
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_CONSUMPTION): _ENERGY_SELECTOR,
                vol.Optional(CONF_EXPORT): _ENERGY_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the source meters without removing the integration."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = {CONF_CONSUMPTION: user_input[CONF_CONSUMPTION]}
            if user_input.get(CONF_EXPORT):
                data[CONF_EXPORT] = user_input[CONF_EXPORT]
            return self.async_update_reload_and_abort(entry, data=data)

        cur = entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONSUMPTION,
                    description={"suggested_value": cur.get(CONF_CONSUMPTION)},
                ): _ENERGY_SELECTOR,
                vol.Optional(
                    CONF_EXPORT, description={"suggested_value": cur.get(CONF_EXPORT)}
                ): _ENERGY_SELECTOR,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EacOptionsFlow(config_entry)


class EacOptionsFlow(OptionsFlow):
    """Manage billing periods, monthly rates and tariff overrides."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._periods: list[dict] = [dict(p) for p in entry.options.get(CONF_PERIODS, [])]
        self._tariff: dict = dict(entry.options.get(CONF_TARIFF, {}))
        self._month_rates: dict = dict(entry.options.get(CONF_MONTH_RATES, {}))
        self._edit_id: str | None = None

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title="",
            data={
                CONF_PERIODS: self._periods,
                CONF_TARIFF: self._tariff,
                CONF_MONTH_RATES: self._month_rates,
            },
        )

    def _period_choices(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(value=p[P_ID], label=p.get(P_NAME, p[P_ID]))
            for p in self._periods
        ]

    # ---- menu -----------------------------------------------------------
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = ["add_period"]
        if self._periods:
            options += ["edit_select", "remove_select"]
        options += ["month_rates", "tariff"]
        return self.async_show_menu(step_id="init", menu_options=options)

    # ---- periods --------------------------------------------------------
    def _period_schema(self, defaults: dict | None = None) -> vol.Schema:
        d = defaults or {}
        return vol.Schema(
            {
                vol.Required(P_NAME, description={"suggested_value": d.get(P_NAME)}): _TEXT_SELECTOR,
                vol.Required(P_START, description={"suggested_value": d.get(P_START)}): _DATE_SELECTOR,
                vol.Required(P_END, description={"suggested_value": d.get(P_END)}): _DATE_SELECTOR,
                vol.Optional(
                    P_RATE_MONTH, description={"suggested_value": d.get(P_RATE_MONTH)}
                ): selector.TextSelector(),  # "YYYY-MM"; blank = period end month
                vol.Optional(
                    P_MANUAL_GROSS, description={"suggested_value": d.get(P_MANUAL_GROSS)}
                ): _number(),  # override gross imported kWh (skips statistics)
                vol.Optional(
                    P_MANUAL_EXPORT, description={"suggested_value": d.get(P_MANUAL_EXPORT)}
                ): _number(),  # solar export kWh, used with manual gross
            }
        )

    @staticmethod
    def _apply_optional(period: dict, user_input: dict) -> None:
        """Copy optional period fields (rate month, manual kWh) into the period."""
        rm = (user_input.get(P_RATE_MONTH) or "").strip()
        if rm:
            period[P_RATE_MONTH] = rm
        else:
            period.pop(P_RATE_MONTH, None)
        for key in (P_MANUAL_GROSS, P_MANUAL_EXPORT):
            if user_input.get(key) is not None:
                period[key] = float(user_input[key])
            else:
                period.pop(key, None)

    async def async_step_add_period(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            period = {
                P_ID: uuid.uuid4().hex[:8],
                P_NAME: user_input[P_NAME],
                P_START: user_input[P_START],
                P_END: user_input[P_END],
            }
            self._apply_optional(period, user_input)
            self._periods.append(period)
            return self._save()
        return self.async_show_form(step_id="add_period", data_schema=self._period_schema())

    async def async_step_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_id = user_input["period"]
            return await self.async_step_edit_period()
        schema = vol.Schema(
            {
                vol.Required("period"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=self._period_choices())
                )
            }
        )
        return self.async_show_form(step_id="edit_select", data_schema=schema)

    async def async_step_edit_period(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = next((p for p in self._periods if p[P_ID] == self._edit_id), None)
        if current is None:
            return await self.async_step_init()
        if user_input is not None:
            current[P_NAME] = user_input[P_NAME]
            current[P_START] = user_input[P_START]
            current[P_END] = user_input[P_END]
            self._apply_optional(current, user_input)
            return self._save()
        return self.async_show_form(
            step_id="edit_period", data_schema=self._period_schema(current)
        )

    async def async_step_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._periods = [p for p in self._periods if p[P_ID] != user_input["period"]]
            return self._save()
        schema = vol.Schema(
            {
                vol.Required("period"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=self._period_choices())
                )
            }
        )
        return self.async_show_form(step_id="remove_select", data_schema=schema)

    # ---- monthly rates --------------------------------------------------
    async def async_step_month_rates(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the production / fuel-adjustment multipliers for one month."""
        if user_input is not None:
            key = user_input["month"].strip()
            entry: dict = {}
            if user_input.get(M_FUEL_C) is not None:
                entry[M_FUEL_C] = float(user_input[M_FUEL_C])
            if user_input.get(M_PRODUCTION) is not None:
                entry[M_PRODUCTION] = float(user_input[M_PRODUCTION])
            if entry:
                self._month_rates[key] = entry
            else:
                self._month_rates.pop(key, None)  # clearing both removes the override
            return self._save()

        schema = vol.Schema(
            {
                vol.Required("month"): _TEXT_SELECTOR,  # "YYYY-MM"
                vol.Optional(M_FUEL_C): _number(),
                vol.Optional(M_PRODUCTION): _number(),
            }
        )
        return self.async_show_form(step_id="month_rates", data_schema=schema)

    # ---- tariff ---------------------------------------------------------
    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._tariff = {k: float(v) for k, v in user_input.items() if v is not None}
            return self._save()

        defaults = Tariff()
        fields: dict = {}
        for name in TARIFF_FIELDS:
            current = self._tariff.get(name, getattr(defaults, name))
            fields[
                vol.Optional(name, description={"suggested_value": current})
            ] = _number()
        return self.async_show_form(step_id="tariff", data_schema=vol.Schema(fields))
