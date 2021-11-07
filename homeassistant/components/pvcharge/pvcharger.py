"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Callable

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change,
    async_track_time_interval,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class PVCharger:
    # pylint: disable=no-member
    """Finite state machine to control the PV charging."""

    states = ["off", "idle", "pv", "max", "low_batt", "calendar"]
    transitions = [
        ["auto", ["off", "max", "low_batt", "calendar"], "low_batt", [], "min_soc"],
        ["auto", ["off", "max", "low_batt", "calendar"], "pv", "enough_power"],
        ["auto", ["off", "max", "low_batt", "calendar"], "idle"],
        ["start", "idle", "pv"],
        ["pause", "pv", "idle"],
        ["boost", "*", "max"],
        ["soc_low", ["idle", "pv"], "low_batt"],
        ["halt", "*", "off"],
    ]

    def __init__(self, hass: HomeAssistant, config) -> None:
        """Set up PVCharger instance."""

        self.hass: HomeAssistant = hass

        for k in config[DOMAIN]:
            setattr(self, k, config[DOMAIN][k])

        self.machine = AsyncMachine(
            model=self,
            states=PVCharger.states,
            transitions=PVCharger.transitions,
            initial="off",
            queued=True,
        )

        self.pid = PID(
            -1.0,
            -0.1,
            0.0,
            setpoint=0.0,
            sample_time=1,
            output_limits=(self.charge_min, self.charge_max),  # type: ignore
        )
        self.control = self.charge_min  # type: ignore
        self.current = float(hass.states.get(self.balance_entity).state)  # type: ignore
        self.soc = float(hass.states.get(self.soc_entity).state)  # type: ignore
        self.pid_interval = self.refresh_interval  # type: ignore
        self.pid_handle: Callable | None = None
        self.timeisup_handle: Callable | None = None

        self._watch_handle: Callable | None = None

    async def _async_update_control(self) -> None:
        await self.hass.services.async_call(
            "input_number",
            "set_value",
            {"entity_id": self.charge_entity, "value": self.control},  # type: ignore
        )

    async def _async_turn_charge_switch(self, value: bool) -> None:
        service = "turn_on" if value else "turn_off"

        await self.hass.services.async_call(
            "input_boolean",
            service,
            target={"entity_id": self.charge_switch},  # type: ignore
        )

    @callback
    async def _async_update_pid(self, event_time) -> None:
        """Update pid controller values."""
        _LOGGER.debug("Call _async_update_pid() callback at %s", event_time)
        self.control = self.pid(self.current)
        _LOGGER.debug(
            "Data is self.current=%s, self.control=%s", self.current, self.control
        )
        await self._async_update_control()

    @callback
    async def _async_watch_balance(self, entity, old_state, new_state) -> None:
        """Watch for changed inputs and act accordingly."""
        _LOGGER.debug("Update changed inputs")

        if entity == self.balance_entity:  # type: ignore
            # Update new grid balance state in memory
            self.current = float(new_state.state)

            # Check if mode change is necessary
            if self.is_pv() and not self.enough_power:  # type: ignore
                await self.pause()  # type: ignore

            if self.is_idle() and self.enough_power:  # type: ignore
                await self.start()  # type: ignore

        if entity == self.soc_entity:  # type: ignore
            # Update new SOC state in instance
            self.soc = float(new_state.state)

            # Check if mode change is necesasry
            if self.is_low_batt() and self.min_soc:  # type: ignore
                await self.auto()  # type: ignore

            if (self.is_idle() or self.is_pv()) and not self.min_soc:  # type: ignore
                await self.soc_low()  # type: ignore

    @callback
    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self.timeisup_handle = None
        await self.auto()  # type: ignore

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""
        threshold = (
            self.pv_threshold - self.pv_hysteresis  # type: ignore
            if self.is_pv()  # type: ignore
            else self.pv_threshold  # type: ignore
        )
        return self.current > threshold

    @property
    def min_soc(self) -> bool:
        """Check if minimal SOC is reached."""
        return self.soc >= self.soc_low_value  # type: ignore

    async def on_exit_off(self) -> None:
        """Register state watcher callback when leaving off mode."""
        self._watch_handle = async_track_state_change(
            self.hass,
            [self.balance_entity, self.soc_entity],  # type: ignore
            self._async_watch_balance,
        )

    async def on_enter_off(self) -> None:
        """Cancel state watcher callback when entering off mode."""
        if self._watch_handle is not None:
            self._watch_handle()
            self._watch_handle = None

    async def on_enter_pv(self, *args, **kwargs) -> None:
        """Start control loop for pv controlled charging."""
        _LOGGER.info("Enter PID charging mode")

        # Start PID mode with minimal charge power
        self.control = self.charge_min  # type: ignore
        await self._async_update_control()
        self.pid.set_auto_mode(True, self.control)

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(True)

        self.pid_handle = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            timedelta(seconds=self.pid_interval),
        )

    async def on_exit_pv(self) -> None:
        """Stop pid loop."""

        self.pid.set_auto_mode(False)

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(False)

        if self.pid_handle is not None:
            self.pid_handle()
            self.pid_handle = None

    async def on_enter_max(self, duration: timedelta = timedelta(minutes=30)) -> None:
        """Load EV battery with maximal power."""

        self.timeisup_handle = async_call_later(
            self.hass, duration, self._async_time_is_up
        )
        self.control = self.charge_max  # type: ignore
        await self._async_update_control()

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(True)

    async def on_exit_max(self) -> None:
        """Cancel pending timeouts."""

        if self.timeisup_handle is not None:
            self.timeisup_handle()
            self.timeisup_handle = None

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(False)

    async def on_enter_low_batt(self) -> None:
        """Charge with max power until min soc is reached."""
        _LOGGER.info("Enter battery low mode")

        self.control = self.charge_max  # type: ignore
        await self._async_update_control()

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(True)

    async def on_exit_low_batt(self) -> None:
        """Turn charge switch off."""

        if self.charge_switch:  # type: ignore
            await self._async_turn_charge_switch(False)
