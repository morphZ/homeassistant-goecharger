"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change,
    async_track_time_interval,
)

from .models import GoeStatus

_LOGGER = logging.getLogger(__name__)

AMP_MIN = 6.0
AMP_MAX = 32.0


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
        charger_host,
        duration,
        soc_entity,
        low_value,
        pid_interval,
    ) -> None:
        """Set up PVCharger instance."""

        self.hass = hass

        self._host = charger_host
        self.duration = duration
        self.soc_entity = soc_entity
        self.low_value = low_value
        self.pid_interval = pid_interval
        self.amp_min = AMP_MIN
        self.amp_max = AMP_MAX

        # Initialize basic controller variables
        self.control = self.amp_min
        self._session = async_create_clientsession(hass)
        self._base_url = f"http://{charger_host}"
        self._charge_power = 0.0
        self._status: GoeStatus | None = None

        try:
            self.soc = float(self.hass.states.get(self.soc_entity).state)  # type: ignore
        except ValueError:
            self.soc = self.low_value

        self._handles: dict[str, Any] = {}

        self.machine = AsyncMachine(
            model=self,
            states=PVCharger.states,
            transitions=PVCharger.transitions,
            initial="off",
            queued=True,
        )

        self.pid = PID(
            0.7,
            0.05,
            0.0,
            setpoint=7.5,
            sample_time=1,
            output_limits=(self.amp_min, self.amp_max),
        )

    async def _async_update_status(self):
        """Read power value of charger from API."""

        async with self._session.get(self._base_url + "/status") as res:
            status = await res.text()

        self._status = GoeStatus.parse_raw(status)
        self._charge_power = round(0.01 * self._status.nrg[11], 2)

    async def _async_watch_soc(
        self,
        entity,
        old_state,
        new_state,
    ) -> None:
        """Update internal soc variable and change state if necessary."""
        self.soc = float(new_state.state)

        # Check if mode change is necesasry
        if self.is_low_batt() and self.min_soc:  # type: ignore
            await self.auto()  # type: ignore

        if (self.is_idle() or self.is_pv()) and not self.min_soc:  # type: ignore
            await self.soc_low()  # type: ignore

    async def _async_update_control(self) -> None:
        amp = round(self.control)
        async with self._session.get(f"{self._base_url}/mqtt?payload=amx={amp}") as res:
            _LOGGER.debug("Response of amp update request: %s", res)

    # async def _async_turn_charge_switch(self, value: bool) -> None:
    #     service = "turn_on" if value else "turn_off"

    #     await self.hass.services.async_call(
    #         "switch",
    #         service,
    #         target={"entity_id": self.charge_switch},
    #     )

    async def _async_update_pid(self, event_time) -> None:
        """Update pid controller values."""
        _LOGGER.debug("Call _async_update_pid() callback at %s", event_time)

        await self._async_update_status()

        setpoint = float(self.hass.states.get("input_number.charge_power").state)  # type: ignore
        self.pid.setpoint = setpoint

        self.control = self.pid(self._charge_power)  # type: ignore
        _LOGGER.debug(
            "Data is self._charge_power=%s, self.control=%s, self.pid.setpoint=%s",
            self._charge_power,
            self.control,
            self.pid.setpoint,
        )
        await self._async_update_control()

    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self._handles.pop("timer", None)
        await self.auto()  # type: ignore

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""

        return True
        # threshold = (
        #     self.pv_threshold - self.pv_hysteresis
        #     if self.is_pv()  # type: ignore
        #     else self.pv_threshold
        # )

        # try:
        #     value = mean(self._balance_store)
        # except StatisticsError:
        #     value = self.current

        # return value > threshold

    @property
    def min_soc(self) -> bool:
        """Check if minimal SOC is reached."""
        return self.soc >= self.low_value

    async def on_exit_off(self) -> None:
        """Register state watcher callback when leaving off mode."""

        # self._handles["balance"] = async_track_state_change(
        #     self.hass,
        #     [self.balance_entity],
        #     self._async_watch_balance,
        # )

        self._handles["soc"] = async_track_state_change(
            self.hass,
            [self.soc_entity],
            self._async_watch_soc,
        )

    async def on_enter_off(self) -> None:
        """Cancel state watcher callback when entering off mode."""
        for handle in ("balance", "soc"):
            self._handles.pop(handle).async_remove()

    async def on_enter_pv(self, *args, **kwargs) -> None:
        """Start control loop for pv controlled charging."""
        _LOGGER.info("Enter PID charging mode")

        # Start PID mode with minimal charge power
        self.control = self.amp_min
        # await self._async_update_control()
        self.pid.set_auto_mode(True, self.control)

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(True)

        self._handles["pid"] = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            timedelta(seconds=self.pid_interval),
        )

    async def on_exit_pv(self) -> None:
        """Stop pid loop."""

        self.pid.set_auto_mode(False)

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(False)

        pid_handle = self._handles.pop("pid", None)

        if pid_handle is not None:
            pid_handle()

    async def on_enter_max(self, duration: timedelta = timedelta(minutes=30)) -> None:
        """Load EV battery with maximal power."""

        self._handles["timer"] = async_call_later(
            self.hass, duration, self._async_time_is_up
        )
        self.control = self.amp_max
        # await self._async_update_control()

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(True)

    async def on_exit_max(self) -> None:
        """Cancel pending timeouts."""

        timer_handle = self._handles.pop("timer", None)
        if timer_handle is not None:
            timer_handle()
            timer_handle = None

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(False)

    async def on_enter_low_batt(self) -> None:
        """Charge with max power until min soc is reached."""
        _LOGGER.info("Enter battery low mode")

        self.control = self.amp_max
        # await self._async_update_control()

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(True)

    async def on_exit_low_batt(self) -> None:
        """Turn charge switch off."""

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(False)
