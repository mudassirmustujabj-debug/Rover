import random

import pytest

from anomaly import ALERT, LEARNING, NORMAL, Channel, Detector, default_detector


def noisy(rng, level, sigma, n):
    return [level + rng.gauss(0, sigma) for _ in range(n)]


def feed(ch, values):
    """Feed values, return list of (index, event)."""
    out = []
    for i, v in enumerate(values):
        ev = ch.update(v)
        if ev:
            out.append((i, ev))
    return out


@pytest.fixture
def rng():
    return random.Random(42)


@pytest.fixture
def ch():
    return Channel("temp", min_sigma=0.5)


def test_steady_noise_stays_quiet(ch, rng):
    events = feed(ch, noisy(rng, 25.0, 0.3, 1000))
    assert events == []
    assert ch.state == NORMAL


def test_never_alerts_during_warmup(rng):
    ch = Channel("gas", min_sigma=30.0, warmup=30)
    # sensor heating up: big decaying values during warmup
    values = [900 - 20 * i for i in range(30)]
    assert feed(ch, values) == []
    assert ch.state == NORMAL


def test_step_change_alerts_then_clears(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    events = feed(ch, noisy(rng, 35.0, 0.3, 20) + noisy(rng, 25.0, 0.3, 20))
    kinds = [e for _, e in events]
    assert kinds == ["alert", "clear"]
    # alert must fire after `confirm` samples, not instantly
    assert events[0][0] == ch.confirm - 1


def test_single_sample_glitch_is_ignored(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    values = noisy(rng, 25.0, 0.3, 10)
    values[5] = 60.0  # one bad reading
    assert feed(ch, values) == []
    assert ch.state == NORMAL


def test_glitch_does_not_poison_baseline(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    before = ch.mean
    feed(ch, [80.0, 80.0])  # two suspect samples, below confirm=3
    feed(ch, noisy(rng, 25.0, 0.3, 5))
    assert abs(ch.mean - before) < 0.5


def test_slow_drift_is_followed_not_flagged(ch, rng):
    # e.g. rover slowly moving into a warmer area: +0.02 C per second
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    drift = [25.0 + 0.02 * i + rng.gauss(0, 0.3) for i in range(600)]
    assert feed(ch, drift) == []
    assert ch.mean > 30.0  # baseline really did follow it


def test_fast_ramp_is_flagged(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    ramp = [25.0 + 0.5 * i for i in range(30)]  # +0.5 C per second
    kinds = [e for _, e in feed(ch, ramp)]
    assert "alert" in kinds


def test_baseline_frozen_during_alert(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    before = ch.mean
    events = feed(ch, noisy(rng, 40.0, 0.3, 60))
    assert [e for _, e in events] == ["alert"]
    assert ch.state == ALERT  # still alerting: it was not learned as normal
    assert abs(ch.mean - before) < 1e-9


def test_hysteresis_no_flicker(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.0, 100))  # sigma -> floor (0.5)
    sigma = ch.sigma()
    # go to alert
    feed(ch, [25.0 + 5 * sigma] * 3)
    assert ch.state == ALERT
    # hover between z_off and z_on: must stay in alert, no events
    hover = [25.0 + 3 * sigma] * 30
    assert feed(ch, hover) == []
    assert ch.state == ALERT


def test_persistent_change_triggers_relearn_then_quiet(rng):
    ch = Channel("temp", min_sigma=0.5, max_alert=50)
    feed(ch, noisy(rng, 20.0, 0.3, 100))
    events = feed(ch, noisy(rng, 32.0, 0.3, 300))  # moved to a hot room for good
    kinds = [e for _, e in events]
    assert kinds[0] == "alert"
    assert "relearn" in kinds
    assert ch.state == NORMAL
    # after relearning, the hot room is the new normal
    assert abs(ch.mean - 32.0) < 1.0
    # and nothing fires after the relearn finished
    relearn_idx = [i for i, e in events if e == "relearn"][0]
    assert all(i <= relearn_idx for i, _ in events)


def test_none_readings_are_ignored(ch, rng):
    values = noisy(rng, 25.0, 0.3, 40)
    for v in values:
        ch.update(None)
        ch.update(v)
    assert ch.n == 40


def test_sensor_preheat_hold_with_none(rng):
    # MQ-2 not ready: sensors.py returns None; channel must stay in LEARNING
    ch = Channel("gas", min_sigma=30.0)
    assert feed(ch, [None] * 100) == []
    assert ch.state == LEARNING and ch.n == 0


def test_reset_relearns(ch, rng):
    feed(ch, noisy(rng, 25.0, 0.3, 100))
    ch.reset()
    assert ch.state == LEARNING
    assert feed(ch, noisy(rng, 50.0, 0.3, 100)) == []
    assert abs(ch.mean - 50.0) < 1.0


def test_min_sigma_prevents_hair_trigger(rng):
    # a perfectly still sensor must not alarm on a tiny wobble
    ch = Channel("hum", min_sigma=2.0)
    feed(ch, [50.0] * 100)
    assert feed(ch, [51.5, 48.5, 51.0, 49.0] * 10) == []


def test_detector_reports_which_channel(rng):
    det = default_detector()
    for _ in range(100):
        det.update({"gas": 400 + rng.gauss(0, 10), "temp": 25 + rng.gauss(0, 0.2),
                    "hum": 50 + rng.gauss(0, 0.5), "dist": 80})
    assert det.alerting() == []
    events = []
    for _ in range(5):
        events += det.update({"gas": 1500, "temp": 25.0, "hum": 50.0})
    assert [(n, e) for n, e, _, _ in events] == [("gas", "alert")]
    assert det.alerting() == ["gas"]
    assert det.states()["temp"] == NORMAL


def test_detector_ignores_unknown_keys():
    det = Detector([Channel("temp", 0.5)])
    assert det.update({"dist": 99, "temp": 25.0}) == []
