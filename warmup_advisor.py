"""
Warm-up advisor: after a cold start, tells the driver when to move off and when it's safe to push.

Stages (each one only moves forward; restarting the engine starts over):

  COLD START     Wait for the fast idle to settle (the change in engine sound drivers listen for):
                 idle RPM steady and back near the normal warm idle, or 60 s at most.
  READY TO MOVE  Drive gently, below 2,500 rpm.
  WARMING        Coolant at 70 °C: moderate driving, below 3,500 rpm.
  READY TO PUSH  Coolant >= 80 °C and oil >= 85 °C, held for a minute: full power OK.

A start with the engine already warm (coolant >= 70 °C) skips straight to the matching stage.

Each stage has an RPM limit (a "variable redline", like BMW M cars) and a sentence to announce.
Thresholds are partly measured (warm idle ~860 rpm from drives on 2026-10-13) and partly general
guidance for petrol engines; tune them after recording a real cold morning start. Note: on the
Tiago the oil temperature follows the coolant very closely, so it may be estimated by the engine
computer rather than measured; the hold time adds a margin for that.
"""

import time
from collections import deque


def _number(value):
    return value if isinstance(value, (int, float)) else None


STAGES = {
    "COLD START": {"level": "warning", "detail": "wait: idle settling", "rpm_limit": 2000,
                   "say": "Cold start. Wait for the idle to settle."},
    "READY TO MOVE": {"level": "good", "detail": "gently, < 2,500 rpm", "rpm_limit": 2500,
                      "say": "Ready to move. Drive gently."},
    "WARMING": {"level": "good", "detail": "moderate, < 3,500 rpm", "rpm_limit": 3500,
                "say": "Engine warming up. Moderate driving."},
    "READY TO PUSH": {"level": "good", "detail": "full power OK", "rpm_limit": None,
                      "say": "Engine warm. Full power OK."},
}
ORDER = list(STAGES)


class WarmUpAdvisor:
    RUNNING_RPM = 400
    COLD_BELOW = 70  # °C coolant at start: below this it's a cold start
    WARM_IDLE_RPM = 860  # normal warm idle (measured)
    SETTLED_MARGIN = 140  # idle within this of the warm idle = fast idle is over
    SETTLED_SPREAD = 100  # ...and steady: max - min over the window
    SETTLE_WINDOW = 5  # seconds of idle readings looked at
    MIN_WAIT, MAX_WAIT = 20, 60  # seconds after starting
    WARMING_COOLANT = 70
    PUSH_COOLANT, PUSH_OIL, PUSH_HOLD = 80, 85, 60  # °C, °C, seconds they must hold

    def __init__(self):
        self.start = None  # time the engine started (this run)
        self.cold = None  # was it a cold start?
        self.stage = None
        self.idle = deque()  # (time, rpm) while idling, for the settle check
        self.push_since = None
        self.pending = None  # sentence to announce
        self.running = False

    def update(self, values):
        now = time.time()
        rpm, speed = _number(values.get("RPM")), _number(values.get("Speed"))
        coolant, oil = _number(values.get("Coolant")), _number(values.get("Oil"))
        if rpm is None:
            return
        running = rpm >= self.RUNNING_RPM
        if not running:
            self.running, self.start, self.stage = False, None, None
            return
        if not self.running:  # just started (or the dashboard started with the engine running)
            run_time = _number(values.get("Engine run time"))
            self.start = now - (run_time or 0)
            self.cold, self.stage, self.idle, self.push_since = None, None, deque(), None
        self.running = True
        if coolant is None:
            return  # need the temperature to judge
        if self.cold is None:
            self.cold = coolant < self.COLD_BELOW

        if speed is not None and speed < 1:
            self.idle.append((now, rpm))
        while self.idle and now - self.idle[0][0] > self.SETTLE_WINDOW:
            self.idle.popleft()

        self._advance(self._target(now, coolant, oil))

    def report(self):
        """{state, level, detail, rpm_limit} for the dashboard, or None while the engine is off."""
        if not self.stage:
            return None
        return {"state": self.stage, **{k: v for k, v in STAGES[self.stage].items() if k != "say"}}

    def announcement(self):
        """The sentence to say for a stage change (once), or None."""
        sentence, self.pending = self.pending, None
        return sentence

    # ------------------------------------------------------------ stages

    def _target(self, now, coolant, oil):
        """The stage the readings justify right now."""
        warm_enough = coolant >= self.PUSH_COOLANT and (oil is None or oil >= self.PUSH_OIL)
        self.push_since = (self.push_since or now) if warm_enough else None
        if not self.cold:  # warm start: no waiting, no hold
            if warm_enough:
                return "READY TO PUSH"
            return "WARMING" if coolant >= self.WARMING_COOLANT else "READY TO MOVE"
        if self.stage in (None, "COLD START") and not self._idle_settled(now):
            return "COLD START"
        if self.push_since and now - self.push_since >= self.PUSH_HOLD:
            return "READY TO PUSH"
        return "WARMING" if coolant >= self.WARMING_COOLANT else "READY TO MOVE"

    def _idle_settled(self, now):
        elapsed = now - self.start
        if elapsed >= self.MAX_WAIT:
            return True
        if elapsed < self.MIN_WAIT or not self.idle or now - self.idle[0][0] < self.SETTLE_WINDOW - 1:
            return False
        rpms = [r for _, r in self.idle]
        return max(rpms) <= self.WARM_IDLE_RPM + self.SETTLED_MARGIN and max(rpms) - min(rpms) <= self.SETTLED_SPREAD

    def _advance(self, target):
        """Move to the target stage, never backwards; announce cold-start stages."""
        if self.stage is not None and ORDER.index(target) <= ORDER.index(self.stage):
            return
        first = self.stage is None
        self.stage = target
        if self.cold or not first:  # a warm start says nothing; a cold start announces every stage
            self.pending = STAGES[target]["say"]
