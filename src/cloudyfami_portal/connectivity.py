"""Online/offline judgement with hysteresis, the same rule scout-lora-qa uses on a base.

Three failures in a row are needed to call a link down and two successes to call it back up, so a
single dropped request does not flip the mode back and forth.
"""
from dataclasses import dataclass, replace
from enum import Enum


class Mode(Enum):
    ONLINE = 'online'
    OFFLINE = 'offline'


@dataclass(frozen=True)
class Connectivity:
    fail_threshold: int = 3
    ok_threshold: int = 2
    mode: Mode = Mode.OFFLINE
    streak: int = 0

    def observe(self, probe_ok):
        """Return the next state. Only a streak against the current mode can flip it."""
        against = probe_ok if self.mode is Mode.OFFLINE else not probe_ok
        if not against:
            return replace(self, streak=0)
        streak = self.streak + 1
        needed = self.ok_threshold if self.mode is Mode.OFFLINE else self.fail_threshold
        if streak < needed:
            return replace(self, streak=streak)
        flipped = Mode.ONLINE if self.mode is Mode.OFFLINE else Mode.OFFLINE
        return replace(self, mode=flipped, streak=0)
