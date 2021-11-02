"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Callable

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(seconds=5)


class PVCharger:
    """Finite state machine to control the PV charging."""

    states = ["off", "idle", "pv", "boost", "calendar"]
    transitions = [
        ["start", "off", "pv"],
        ["battery_low", "*", "boost"],
        ["battery_ok", "boost", "pv"],
        ["calendar_event", ["off", "pv"], "calendar"],
        ["touch", ["pv", "calendar"], "="],
        ["off", "*", "off"],
    ]

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up PVCharger instance."""

        self.hass: HomeAssistant = hass

        self.machine = AsyncMachine(
            model=self,
            states=PVCharger.states,
            transitions=PVCharger.transitions,
            initial="off",
        )

        self.pid = PID(
            -1.0, -0.1, 0.0, setpoint=0.0, sample_time=1, output_limits=(2.0, 11.0)
        )
        self.control: float | None = 0.0
        self.pid_handle: Callable | None = None
        self.pid_interval: timedelta = REFRESH_INTERVAL

    async def on_enter_pv(self) -> None:
        """Start control loop for pv controlled charging."""

        @callback
        async def update_pid(event_time) -> None:
            _LOGGER.info("Call update_pid() callback at %s", event_time)
            current = float(self.hass.states.get("input_number.grid_return").state)  # type: ignore
            self.control = self.pid(current)
            _LOGGER.info("Data is current=%s, self.control=%s", current, self.control)

        self.pid_handle = async_track_time_interval(
            self.hass,
            update_pid,
            self.pid_interval,
        )

    async def on_exit_pv(self) -> None:
        """Stop pid loop."""

        if self.pid_handle is not None:
            self.pid_handle()
            self.pid_handle = None
