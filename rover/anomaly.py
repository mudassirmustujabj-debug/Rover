"""Rolling-baseline anomaly detector for the inspection rover.

Pure Python: no machine/network imports, no dataclasses/statistics/typing,
so the SAME file runs under pytest on a laptop and under MicroPython on the ESP32.

How a channel behaves (one channel = one sensor value, e.g. "gas"):

  LEARNING  first `warmup` samples: just learn what normal looks like. Never alerts.
  NORMAL    compare each sample to the baseline (mean, sigma). Baseline adapts slowly.
  ALERT     entered after `confirm` consecutive samples at |z| >= z_on.
            Left after `clear` consecutive samples at |z| < z_off (hysteresis).
            Baseline is FROZEN while alerting, so a growing problem is never
            learned as "normal".
            If an alert lasts `max_alert` samples, the environment has probably
            changed for real (rover drove into another room): the channel
            re-learns from scratch and reports "relearn".

Feed update() one reading per sample. A reading of None (sensor not ready or
read failed) is ignored, which is also how you hold off the MQ-2 until its
heater has warmed up.
"""

import math

LEARNING = "learning"
NORMAL = "normal"
ALERT = "alert"


class Channel:
    def __init__(self, name, min_sigma, z_on=4.0, z_off=2.0, alpha=0.02,
                 warmup=30, confirm=3, clear=5, max_alert=120):
        self.name = name
        self.min_sigma = min_sigma  # noise floor in the sensor's own units
        self.z_on = z_on
        self.z_off = z_off
        self.alpha = alpha          # baseline adaptation speed in NORMAL
        self.warmup = warmup
        self.confirm = confirm
        self.clear = clear
        self.max_alert = max_alert
        self.reset()

    def reset(self):
        """Forget the baseline and start learning again."""
        self.state = LEARNING
        self.n = 0
        self.mean = 0.0
        self.var = 0.0
        self.z = 0.0
        self._hi = 0
        self._lo = 0
        self._age = 0

    def sigma(self):
        s = math.sqrt(self.var)
        return s if s > self.min_sigma else self.min_sigma

    def _learn(self, x):
        # Running mean/variance. 1/n at first (true average), then fixed alpha.
        self.n += 1
        a = 1.0 / self.n
        if a < self.alpha:
            a = self.alpha
        d = x - self.mean
        self.mean += a * d
        self.var = (1.0 - a) * (self.var + a * d * d)

    def update(self, x):
        """Process one reading. Returns "alert", "clear", "relearn" or None."""
        if x is None:
            return None

        if self.state == LEARNING:
            self._learn(x)
            self.z = 0.0
            if self.n >= self.warmup:
                self.state = NORMAL
            return None

        self.z = (x - self.mean) / self.sigma()
        az = abs(self.z)

        if self.state == NORMAL:
            if az >= self.z_on:
                # Suspicious sample: count it, but do NOT learn from it.
                self._hi += 1
                if self._hi >= self.confirm:
                    self.state = ALERT
                    self._hi = 0
                    self._lo = 0
                    self._age = 0
                    return "alert"
                return None
            self._hi = 0
            self._learn(x)
            return None

        # state == ALERT (baseline frozen)
        self._age += 1
        if az < self.z_off:
            self._lo += 1
            if self._lo >= self.clear:
                self.state = NORMAL
                self._lo = 0
                return "clear"
        else:
            self._lo = 0
        if self._age >= self.max_alert:
            self.reset()
            self._learn(x)
            return "relearn"
        return None


class Detector:
    """A set of channels, updated together once per sampling tick."""

    def __init__(self, channels):
        self.channels = {}
        for c in channels:
            self.channels[c.name] = c

    def update(self, readings):
        """readings: dict name -> value (or None).
        Returns a list of (name, event, value, z) tuples for this tick."""
        events = []
        for name in self.channels:
            if name in readings:
                ch = self.channels[name]
                ev = ch.update(readings[name])
                if ev:
                    events.append((name, ev, readings[name], ch.z))
        return events

    def states(self):
        out = {}
        for name in self.channels:
            out[name] = self.channels[name].state
        return out

    def alerting(self):
        out = []
        for name in self.channels:
            if self.channels[name].state == ALERT:
                out.append(name)
        return out

    def reset(self):
        for name in self.channels:
            self.channels[name].reset()


def default_detector():
    """Starting values. min_sigma numbers are GUESSES: tune them on the real
    sensors (log a quiet minute, set min_sigma near the observed noise).

    NOTE: distance is deliberately NOT a channel. The rover moves, so distance
    changes legitimately all the time. Use it for an obstacle warning instead.
    """
    return Detector([
        Channel("gas", min_sigma=30.0),   # raw ADC counts (0-4095)
        Channel("temp", min_sigma=0.5),   # deg C
        Channel("hum", min_sigma=2.0),    # % RH
    ])
