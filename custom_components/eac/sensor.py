"""Sensor platform: one sensor per EAC bill line item, per billing period."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CURRENT_ID, DOMAIN, EUR, KWH, P_ID, P_NAME, RATE_UNIT, SENSOR_FIELDS
from .coordinator import EacCoordinator, PeriodData

PRODUCTION_RATE_UNIT = "€/kWh"


def _description(key: str, kind: str) -> SensorEntityDescription:
    if kind == "money":
        return SensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=SensorDeviceClass.MONETARY,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=EUR,
            suggested_display_precision=2,
        )
    if kind == "energy":
        return SensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=KWH,
            suggested_display_precision=2,
        )
    if kind == "prate":
        return SensorEntityDescription(
            key=key,
            translation_key=key,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PRODUCTION_RATE_UNIT,
            suggested_display_precision=4,
        )
    return SensorEntityDescription(  # "rate" (fuel adjustment ¢/kWh)
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=RATE_UNIT,
        suggested_display_precision=4,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create line-item sensors for every configured billing period."""
    coordinator: EacCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[EacBillSensor] = []
    for period in coordinator.all_periods():
        for key, kind in SENSOR_FIELDS:
            entities.append(
                EacBillSensor(coordinator, entry, period, _description(key, kind), kind)
            )
    async_add_entities(entities)


class EacBillSensor(CoordinatorEntity[EacCoordinator], SensorEntity):
    """A single EAC bill line item for one billing period."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EacCoordinator,
        entry: ConfigEntry,
        period: dict,
        description: SensorEntityDescription,
        kind: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._period_id = period[P_ID]
        self._kind = kind
        self._attr_unique_id = f"{entry.entry_id}_{period[P_ID]}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{period[P_ID]}")},
            name=f"EAC {period[P_NAME]}",
            manufacturer="EAC / ΑΗΚ",
            model="Billing period",
        )

    @property
    def _data(self) -> PeriodData | None:
        return (self.coordinator.data or {}).get(self._period_id)

    @property
    def available(self) -> bool:
        data = self._data
        if data is None or data.bill is None:
            return False
        # Money and fuel-rate sensors are meaningless without a fuel rate.
        if self._kind in ("money", "rate") and not data.has_fuel:
            return False
        return super().available

    @property
    def native_value(self):
        data = self._data
        if data is None or data.bill is None:
            return None
        return getattr(data.bill, self.entity_description.key, None)

    @property
    def extra_state_attributes(self) -> dict | None:
        # Surface period metadata on the headline total sensor only.
        if self.entity_description.key != "total":
            return None
        data = self._data
        if data is None:
            return None
        bill = data.bill
        return {
            "is_current_period": self._period_id == CURRENT_ID,
            "period_start": data.start,
            "period_end": data.end,
            "rate_month": data.rate_month,
            "gross_entity": data.gross_entity,
            "net_entity": data.net_entity,
            "data_available_from": data.data_start,
            "data_available_to": data.data_end,
            "coverage_complete": data.coverage_complete,
            "fuel_rate_source": data.fuel_source,
            "production_rate_source": data.prod_source,
            "fuel_rate_c_per_kwh": getattr(bill, "fuel_rate_c", None) if bill else None,
            "production_rate_eur_per_kwh": getattr(bill, "production_rate", None) if bill else None,
            "gross_imported_kwh": getattr(bill, "gross_kwh", None) if bill else None,
            "net_imported_kwh": getattr(bill, "net_kwh", None) if bill else None,
            "offset_kwh": getattr(bill, "offset_kwh", None) if bill else None,
            "error": data.error,
        }
