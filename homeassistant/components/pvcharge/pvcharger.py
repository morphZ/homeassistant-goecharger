"""Logic and code to run pvcharge."""
from __future__ import annotations

from collections import deque
from datetime import timedelta
import logging
from statistics import mean
from typing import Any

from aiohttp.client_exceptions import ClientConnectorError
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
POWER_MIN = 2.0
POWER_MAX = 11.0
SOC_MIN = 0.25
SOC_MID = 0.5
SOC_HIGH1 = 0.7
SOC_HIGH2 = 0.9
PV_UPDATE_INTERVAL = 20
PV_OFFSET = 0.5


class PVCharger:
    # pylint: disable=no-member
    """Finite state machine to control the PV charging."""

    states = ["off", "loading", "boosting", "idle"]
    transitions = [
        ["run", ["off", "max", "idle"], "loading"],
        ["pause", "loading", "idle"],
        ["boost", ["load", "idle"], "boosting"],
        ["halt", "*", "off"],
    ]

    def __init__(
        self,
        hass: HomeAssistant,
        charger_host,
        duration,
        balance_entity,
        soc_entity,
        low_value,
        pid_interval,
    ) -> None:
        """Set up PVCharger instance."""

        self.hass = hass

        self._host = charger_host
        self.duration = duration
        self._balance_entity = balance_entity
        self._soc_entity = soc_entity
        self.low_value = low_value
        self._pid_interval = pid_interval
        self._pv_interval = PV_UPDATE_INTERVAL
        self.amp_min = AMP_MIN
        self.amp_max = AMP_MAX
        self._power_limits = (POWER_MIN, POWER_MAX)
        self._soc_limits = (SOC_MIN, SOC_MID, SOC_HIGH1, SOC_HIGH2)
        self._balance_store: deque = deque(
            [], max(1, self._pv_interval // self._pid_interval)
        )
        self._balance_mean = 0.0

        # Initialize basic controller variables
        self.control = self.amp_min
        self._session = async_create_clientsession(hass)
        self._base_url = f"http://{charger_host}"
        self._charge_power = 0.0
        self._status: GoeStatus | None = None

        try:
            self.soc = float(self.hass.states.get(self._soc_entity).state) / 100.0  # type: ignore
        except ValueError:
            self.soc = 0.48

        try:
            self._balance = float(self.hass.states.get(self._balance_entity).state)  # type: ignore
            self._balance_store.append(self._balance)
        except ValueError:
            self._balance = 0.0

        self._enough_pv = False

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

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""
        offset = PV_OFFSET if self.is_loading() else 0.0  # type: ignore

        if self.soc < self._soc_limits[1]:
            return True
        elif self.soc < self._soc_limits[2]:
            return self._balance_mean > 0.0 - offset
        elif self.soc < self._soc_limits[3]:
            return self._balance_mean > 1.0 - offset
        else:
            return self._balance_mean > 2.0 - offset

    @property
    def car_ready(self) -> bool:
        """Check if car is ready to be charged."""
        return self._status.car not in [1]  # type: ignore

    def _calculate_setpoint(self):
        """Calculate power setpoint from soc."""

        soc_min, soc_mid = self._soc_limits[:2]
        p_min, p_max = self._power_limits
        grad = (p_max - p_min) / (soc_mid - soc_min)

        # Calculate setpoint with respect to SOC and state
        if self.is_boosting():
            power = p_max
        elif self.soc < soc_min:
            power = p_max
        elif self.soc < soc_mid:
            power = p_max - grad * (self.soc - soc_min)
        else:
            power = p_min

        return power

    async def _async_update_status(self):
        """Read power value of charger from API."""

        try:
            async with self._session.get(self._base_url + "/status") as res:
                status = await res.text()
        except ClientConnectorError as exc:
            _LOGGER.exception(exc.msg, exc_info=exc)

        self._status = GoeStatus.parse_raw(status)
        self._charge_power = round(0.01 * self._status.nrg[11], 2)

        self._balance_store.append(self._balance + self._charge_power)

    async def _async_watch_entities(
        self,
        entity,
        old_state,
        new_state,
    ) -> None:
        """Update internal soc variable."""

        if entity == self._soc_entity:
            self.soc = float(new_state.state) / 100.0

        if entity == self._balance_entity:
            self._balance = float(new_state.state)

    async def _async_switch_charger(self, on: bool = True) -> None:
        """Switch go-e charger on or off via API call."""
        alw = 1 if on else 0

        async with self._session.get(f"{self._base_url}/mqtt?payload=alw={alw}") as res:
            _LOGGER.debug("Response of alw update request: %s", res)

    async def _async_update_control(self) -> None:
        """Update PID and set charging current via API call."""
        self.pid.setpoint = self._calculate_setpoint()
        self.control = self.pid(self._charge_power)  # type: ignore
        _LOGGER.debug(
            "New data is self._charge_power=%s, self.control=%s, self.pid.setpoint=%s",
            self._charge_power,
            self.control,
            self.pid.setpoint,
        )

        async with self._session.get(
            f"{self._base_url}/mqtt?payload=amx={round(self.control)}"
        ) as res:
            _LOGGER.debug("Response of amp update request: %s", res)

    async def _async_update_pv(self, event_time) -> None:
        """Update PV generation status (enough / not enough)."""

        _LOGGER.debug("Call _async_update_pv() callback at %s", event_time)

        self._balance_mean = mean(self._balance_store)

        _LOGGER.debug(
            "New data is _balance_store=%s, _balance_mean=%s",
            self._balance_store,
            self._balance_mean,
        )

    async def _async_update_pid(self, event_time) -> None:
        """Update pid controller values."""
        _LOGGER.debug("Call _async_update_pid() callback at %s", event_time)

        await self._async_update_status()

        # Check for changes while in idle mode
        if self.is_idle():  # type: ignore
            if self.enough_power and self.car_ready:
                await self.run()  # type: ignore

        # Check for changes while in loading state
        if self.is_loading():  # type: ignore
            if not (self.enough_power and self.car_ready):
                await self.pause()  # type: ignore
                return

            await self._async_update_control()

    async def on_exit_off(self) -> None:
        """Register callbacks when leaving off mode."""

        self._handles["soc"] = async_track_state_change(
            self.hass,
            [self._soc_entity, self._balance_entity],
            self._async_watch_entities,
        )

        self._handles["pid"] = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            timedelta(seconds=self._pid_interval),
        )

        self._handles["pv"] = async_track_time_interval(
            self.hass,
            self._async_update_pv,
            timedelta(seconds=self._pv_interval),
        )

        await self._async_switch_charger(True)

    async def on_enter_off(self) -> None:
        """Cancel callbacks when entering off mode."""
        for unsub in self._handles.values():
            unsub()

        self._handles = {}

        await self._async_switch_charger(False)

    async def on_enter_idle(self) -> None:
        """Switch off charger and PID controller."""
        await self._async_switch_charger(False)
        self.pid.set_auto_mode(False)

    async def on_exit_idle(self) -> None:
        """Switch on charger and PID controller."""
        await self._async_switch_charger(True)
        self.pid.set_auto_mode(True, self._power_limits[0])

    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self._handles.pop("timer", None)
        await self.auto()  # type: ignore

    async def on_enter_boosting(
        self, duration: timedelta = timedelta(minutes=30)
    ) -> None:
        """Load EV battery with maximal power."""

        self._handles["timer"] = async_call_later(
            self.hass, duration, self._async_time_is_up
        )
        self.control = self.amp_max
        # await self._async_update_control()

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(True)

    async def on_exit_boosting(self) -> None:
        """Cancel pending timeouts."""

        timer_handle = self._handles.pop("timer", None)
        if timer_handle is not None:
            timer_handle()
            timer_handle = None

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(False)
