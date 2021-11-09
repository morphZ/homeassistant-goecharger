"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Callable

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (  # async_track_state_change,
    TrackTemplate,
    async_call_later,
    async_track_template_result,
    async_track_time_interval,
)

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

    def __init__(
        self,
        hass: HomeAssistant,
        balance_template,
        charge_switch,
        charge_min,
        charge_max,
        pv_threshold,
        pv_hysteresis,
        duration,
        soc_template,
        low_value,
        pid_interval,
    ) -> None:
        """Set up PVCharger instance."""

        self.hass = hass
        self.balance_template = balance_template
        self.balance_template.hass = hass
        self.charge_switch = charge_switch
        self.charge_min = charge_min
        self.charge_max = charge_max
        self.pv_threshold = pv_threshold
        self.pv_hysteresis = pv_hysteresis
        self.duration = duration
        self.soc_template = soc_template
        self.soc_template.hass = hass
        self.low_value = low_value
        self.pid_interval = pid_interval

        # Initialize basic controller variables
        self.control = charge_min
        self.current = float(self.balance_template.async_render())
        self.soc = float(self.soc_template.async_render())

        self._handles: dict[str, Any] = {}

        self.pid_handle: Callable | None = None
        self.timeisup_handle: Callable | None = None
        self._watch_handle: Callable | None = None

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
            output_limits=(self.charge_min, self.charge_max),
        )

    @callable
    async def _async_watch_balance(self, event, updates) -> None:
        track_template_result = updates.pop()
        # template = track_template_result.template
        result = track_template_result.result
        # entity = event and event.data.get("entity_id")

        self.current = float(result)

        # Check if mode change is necessary
        if self.is_pv() and not self.enough_power:  # type: ignore
            await self.pause()  # type: ignore

        if self.is_idle() and self.enough_power:  # type: ignore
            await self.start()  # type: ignore

    @callable
    async def _async_watch_soc(self, event, updates) -> None:
        track_template_result = updates.pop()
        # template = track_template_result.template
        result = track_template_result.result
        # entity = event and event.data.get("entity_id")

        self.soc = float(result)

        # Check if mode change is necesasry
        if self.is_low_batt() and self.min_soc:  # type: ignore
            await self.auto()  # type: ignore

        if (self.is_idle() or self.is_pv()) and not self.min_soc:  # type: ignore
            await self.soc_low()  # type: ignore

    async def _async_update_control(self) -> None:
        await self.hass.services.async_call(
            "goecharger",
            "set_max_current",
            {"max_current": self.control},
        )

    async def _async_turn_charge_switch(self, value: bool) -> None:
        service = "turn_on" if value else "turn_off"

        await self.hass.services.async_call(
            "switch",
            service,
            target={"entity_id": self.charge_switch},
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

    # @callback
    # async def _async_watch_balance(self, entity, old_state, new_state) -> None:
    #     """Watch for changed inputs and act accordingly."""
    #     _LOGGER.debug("Update changed inputs")

    #     if entity == self.balance_entity:  # type: ignore
    #         # Update new grid balance state in memory
    #         self.current = float(new_state.state)

    #         # Check if mode change is necessary
    #         if self.is_pv() and not self.enough_power:  # type: ignore
    #             await self.pause()  # type: ignore

    #         if self.is_idle() and self.enough_power:  # type: ignore
    #             await self.start()  # type: ignore

    #     if entity == self.soc_entity:  # type: ignore
    #         # Update new SOC state in instance
    #         self.soc = float(new_state.state)

    #         # Check if mode change is necesasry
    #         if self.is_low_batt() and self.min_soc:  # type: ignore
    #             await self.auto()  # type: ignore

    #         if (self.is_idle() or self.is_pv()) and not self.min_soc:  # type: ignore
    #             await self.soc_low()  # type: ignore

    @callback
    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self.timeisup_handle = None
        await self.auto()  # type: ignore

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""
        threshold = (
            self.pv_threshold - self.pv_hysteresis
            if self.is_pv()  # type: ignore
            else self.pv_threshold
        )
        return self.current > threshold

    @property
    def min_soc(self) -> bool:
        """Check if minimal SOC is reached."""
        return self.soc >= self.low_value

    async def on_exit_off(self) -> None:
        """Register state watcher callback when leaving off mode."""
        # self._watch_handle = async_track_state_change(
        #     self.hass,
        #     [self.balance_entity, self.soc_entity],  # type: ignore
        #     self._async_watch_balance,
        # )

        self._handles["balance"] = async_track_template_result(
            self.hass,
            [TrackTemplate(self.balance_template, None)],
            self._async_watch_balance,  # type: ignore
        )

        self._handles["soc"] = async_track_template_result(
            self.hass,
            [TrackTemplate(self.soc_template, None)],
            self._async_watch_soc,  # type: ignore
        )

    async def on_enter_off(self) -> None:
        """Cancel state watcher callback when entering off mode."""
        for t in ["balance", "soc"]:
            self._handles.pop(t).async_remove()

        # if self._watch_handle is not None:
        #     self._watch_handle()
        #     self._watch_handle = None

    async def on_enter_pv(self, *args, **kwargs) -> None:
        """Start control loop for pv controlled charging."""
        _LOGGER.info("Enter PID charging mode")

        # Start PID mode with minimal charge power
        self.control = self.charge_min
        await self._async_update_control()
        self.pid.set_auto_mode(True, self.control)

        if self.charge_switch:
            await self._async_turn_charge_switch(True)

        self.pid_handle = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            timedelta(seconds=self.pid_interval),
        )

    async def on_exit_pv(self) -> None:
        """Stop pid loop."""

        self.pid.set_auto_mode(False)

        if self.charge_switch:
            await self._async_turn_charge_switch(False)

        if self.pid_handle is not None:
            self.pid_handle()
            self.pid_handle = None

    async def on_enter_max(self, duration: timedelta = timedelta(minutes=30)) -> None:
        """Load EV battery with maximal power."""

        self.timeisup_handle = async_call_later(
            self.hass, duration, self._async_time_is_up
        )
        self.control = self.charge_max
        await self._async_update_control()

        if self.charge_switch:
            await self._async_turn_charge_switch(True)

    async def on_exit_max(self) -> None:
        """Cancel pending timeouts."""

        if self.timeisup_handle is not None:
            self.timeisup_handle()
            self.timeisup_handle = None

        if self.charge_switch:
            await self._async_turn_charge_switch(False)

    async def on_enter_low_batt(self) -> None:
        """Charge with max power until min soc is reached."""
        _LOGGER.info("Enter battery low mode")

        self.control = self.charge_max
        await self._async_update_control()

        if self.charge_switch:
            await self._async_turn_charge_switch(True)

    async def on_exit_low_batt(self) -> None:
        """Turn charge switch off."""

        if self.charge_switch:
            await self._async_turn_charge_switch(False)
