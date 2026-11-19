"""Climate platform for Midea Cloud Control."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    FAN_TO_SPEED,
    HVAC_TO_MODE,
    MAX_TEMP,
    MIN_TEMP,
    MODE_TO_HVAC,
    MODE_TO_SWING,
    SPEED_TO_FAN,
    SWING_TO_MODE,
)
from .coordinator import MideaCloudCoordinator

if TYPE_CHECKING:
    from . import MideaCloudRuntimeData


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate entity for a config entry."""
    runtime: MideaCloudRuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [MideaCloudClimate(coordinator, entry) for coordinator in runtime.coordinators]
    )


class MideaCloudClimate(CoordinatorEntity[MideaCloudCoordinator], ClimateEntity):
    """A Midea air conditioner controlled through the vendor cloud."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, *MODE_TO_HVAC.values()]
    _attr_fan_modes: ClassVar[list[str]] = list(FAN_TO_SPEED)
    _attr_swing_modes: ClassVar[list[str]] = list(SWING_TO_MODE)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: MideaCloudCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = str(coordinator.appliance_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.appliance_id))},
            name=coordinator.appliance_name,
            manufacturer="Midea",
            model="Air conditioner (cloud control)",
        )

    @property
    def _state(self) -> Any:
        return self.coordinator.data

    @property
    def current_temperature(self) -> float | None:
        return getattr(self._state, "indoor_temperature", None)

    @property
    def target_temperature(self) -> float | None:
        return getattr(self._state, "target_temperature", None)

    @property
    def hvac_mode(self) -> HVACMode:
        if not getattr(self._state, "power_on", False):
            return HVACMode.OFF
        return MODE_TO_HVAC.get(self._state.operational_mode, HVACMode.AUTO)

    @property
    def fan_mode(self) -> str | None:
        speed = getattr(self._state, "fan_speed", None)
        if speed is None:
            return None
        # Custom speeds are reported verbatim; snap to the nearest named speed.
        if speed in SPEED_TO_FAN:
            return SPEED_TO_FAN[speed]
        return SPEED_TO_FAN[min(SPEED_TO_FAN, key=lambda s: abs(s - speed))]

    @property
    def swing_mode(self) -> str | None:
        return MODE_TO_SWING.get(getattr(self._state, "swing_mode", None))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "outdoor_temperature": getattr(self._state, "outdoor_temperature", None),
            "eco": getattr(self._state, "eco", None),
            "turbo": getattr(self._state, "turbo", None),
            "sleep": getattr(self._state, "sleep", None),
            "filter_alert": getattr(self._state, "filter_alert", None),
            "error_code": getattr(self._state, "error_code", None),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_state(target_temperature=float(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_set_state(power_on=False)
            return
        await self.coordinator.async_set_state(
            power_on=True, operational_mode=HVAC_TO_MODE[hvac_mode]
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.coordinator.async_set_state(fan_speed=FAN_TO_SPEED[fan_mode])

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self.coordinator.async_set_state(swing_mode=SWING_TO_MODE[swing_mode])

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_state(power_on=True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_state(power_on=False)
