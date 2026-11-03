# Rover <-> Dashboard protocol

One WebSocket at `ws://<host>/ws`. Every message is one JSON object with a `"t"` (type) field.
Rover address when it hosts its own WiFi: `http://192.168.4.1/` (MicroPython/ESP32 default AP).
The fake rover (`tools/fake_rover.py`) speaks exactly this protocol on `http://localhost:8080/`.

If you change a message, change THIS file in the same commit.

## Phone -> rover

### `drive`  (send 10-20 times per second while the joystick is held)
```json
{"t": "drive", "x": 0.4, "y": -0.8}
```
- `x`: steer, -1 (full left) .. +1 (full right)
- `y`: throttle, -1 (full reverse) .. +1 (full forward)
- Both clamped to [-1, 1] by the rover. Joystick centred = `0, 0`.
- Rover mixes to tracks (in `motors.py`): `left = y + x`, `right = y - x`, then scales so neither exceeds 1.
- **Failsafe:** if no `drive` arrives for 500 ms the rover stops both motors. The phone must keep
  sending (even `0, 0`) while connected.

### `reset_baseline`
```json
{"t": "reset_baseline"}
```
Rover forgets all baselines and re-learns (use after driving into a different room).

## Rover -> phone

### `telemetry`  (once per second)
```json
{"t": "telemetry", "ts": 1731000000,
 "gas": 412, "temp": 28.4, "hum": 51.2, "dist": 87,
 "state": {"gas": "normal", "temp": "learning", "hum": "normal"},
 "drive": {"l": 0.0, "r": 0.0}}
```
- `gas`: raw ADC counts (0-4095), relative reading, NOT ppm.
- `temp` deg C, `hum` % RH, `dist` cm (obstacle distance; not part of anomaly detection).
- Any sensor value may be `null` (not ready / read failed). DHT22 values only change every ~2 s.
- `state` per anomaly channel: `"learning"` | `"normal"` | `"alert"`.
- `ts`: seconds. On the ESP32 without a clock this is uptime seconds; the dashboard should treat it as ordering only.

### `alert`  (event, sent when a channel changes state)
```json
{"t": "alert", "ts": 1731000042, "sensor": "gas", "kind": "alert", "value": 1480, "z": 9.3}
```
- `kind`: `"alert"` (abnormal), `"clear"` (back to normal), `"relearn"` (alert lasted too long;
  rover threw away the baseline and is learning the new surroundings).
- Buzzer + LED are on while ANY channel is in `alert`.

### `history`  (once, right after a client connects)
```json
{"t": "history", "alerts": [ {"t": "alert", "ts": 1731000042, "sensor": "gas", "kind": "alert", "value": 1480, "z": 9.3} ]}
```
Up to the last 50 alert events (ring buffer on the rover), oldest first.

## Video
Not on this socket. The ESP32-CAM serves its own MJPEG stream; the dashboard embeds it as
`<img src="http://<cam-ip>:81/stream">`. In the fake rover the camera is just a placeholder.
