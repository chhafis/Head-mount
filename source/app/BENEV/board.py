"""Board automation: status LEDs, fan, the record and power buttons, and running on battery.

  PWR_ON_G   GPIO23  on while the Pi runs: a kernel LED (config.txt gpio-led overlay) that goes
                     off at the very end of shutdown, i.e. when it is safe to unplug
  REC_LED    GPIO24  on while recording; blinks fast while a recording starts or stops, and
                     flickers for 2 s if starting failed
  BAT_LOW_R  GPIO16  on below battery_low_pct; blinks slowly while the Pi runs on battery and
                     counts down to the automatic shutdown
  FAN_CTRL   GPIO12  fan_normal_pct from start-up; full speed from fan_full_c until the CPU is
                     back under fan_back_c
  CAM_REC    GPIO4   record button SW1 (R10 pull-up, pressed = low): start / stop a recording
  PWR_SW     GPIO22  power button SW2 through the LTC2954's INT (low pulse >= 32 ms): safe
                     shutdown. Needs U13 (LTC2954) fitted.
  PI_KILL    GPIO5   to the LTC2954's KILL (R92, R58 pull-up): pulled low at the very end of a
                     power-off by system/benev-poweroff.sh, so U13 switches the board off. Not
                     driven while the Pi runs (low then would switch it off at once).
  BAT_PG     GPIO6   charger PGOOD (low = USB-C power present). After unplug_shutdown_s
                     without it, recordings are saved and the Pi shuts down.
Pins from the bring-up procedure (Pi_Zero_2W_Cam_Board_Bringup_Procedure_01, J7 pin map).
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import gpiod

log = logging.getLogger("dashboard")

RECORD_BUTTON, POWER_BUTTON, POWER_GOOD = 4, 22, 6
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
DEFAULTS = {"fan_normal_pct": 50, "fan_full_c": 60.0, "fan_back_c": 52.0,
            "battery_low_pct": 15, "unplug_shutdown_s": 120}
LIMITS = {"fan_normal_pct": (0, 100), "fan_full_c": (45, 80), "fan_back_c": (35, 77),
          "battery_low_pct": (5, 50), "unplug_shutdown_s": (0, 3600)}
TICK_S = 0.05
BUTTON_DEBOUNCE_S = 0.03      # still pressed this long after the edge = a real press
BUTTON_LOCKOUT_S = 1.0        # ignore further presses for this long
POWER_BUTTON_MIN_S = 0.02     # LTC2954 INT pulses are at least 32 ms
UNPLUG_GLITCH_S = 1.0         # PGOOD must stay released this long to count as unplugged
BATTERY_HYSTERESIS_PCT = 3


def _blink(hz, now):
    return int(now * hz * 2) % 2 == 0


class Board(threading.Thread):
    def __init__(self, outputs, settings_file, app):
        super().__init__(daemon=True, name="board")
        self.outputs = outputs
        self.app = app
        self.settings_file = Path(settings_file)
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(self._validate(json.loads(self.settings_file.read_text())))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring %s: %s", self.settings_file, e)
        self.lines = None
        self.input_error = None
        self.external_power = None   # True / False; None = unknown (input not readable)
        self.released_at = None      # monotonic time PGOOD was released (power removed)
        self.cpu_temp = None
        self.fan_full = False
        self.battery_low = False
        self.action = None           # "starting" / "stopping" while a button action runs
        self.failed_until = 0.0      # REC_LED flickers until then after a failed start
        self.presses = {"record": 0, "power": 0}
        self._last_press = {RECORD_BUTTON: 0.0, POWER_BUTTON: 0.0}
        self._next_temp = 0.0
        try:
            chip = gpiod.Chip("gpiochip0")
            self.lines = chip.get_lines([RECORD_BUTTON, POWER_BUTTON, POWER_GOOD])
            self.lines.request(consumer="benev-dashboard", type=gpiod.LINE_REQ_EV_BOTH_EDGES,
                               flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)
        except Exception as e:
            self.lines = None
            self.input_error = f"{type(e).__name__}: {e}"
            log.warning("board inputs unavailable: %s", self.input_error)

    # ----- settings

    @staticmethod
    def _validate(changes):
        out = {}
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            lo, hi = LIMITS[key]
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            out[key] = type(DEFAULTS[key])(value)
        return out

    def update_settings(self, changes):
        merged = {**self.settings, **self._validate(changes)}
        if merged["fan_back_c"] > merged["fan_full_c"] - 3:
            raise ValueError("fan_back_c must be at least 3 °C below fan_full_c")
        self.settings = merged
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2))
        os.replace(tmp, self.settings_file)
        return self.status()

    # ----- loop

    def run(self):
        while True:
            try:
                self._handle_events()
                now = time.monotonic()
                self._read_power(now)
                self._update_outputs(now)
                self._check_unplugged(now)
            except Exception:
                log.exception("board")
                time.sleep(1)

    def _handle_events(self):
        if self.lines is None:
            time.sleep(TICK_S)
            return
        ready = self.lines.event_wait(nsec=int(TICK_S * 1e9))
        if not ready:
            return
        for line in ready:
            for ev in line.event_read_multiple():
                if ev.type != gpiod.LineEvent.FALLING_EDGE:
                    continue
                gpio = line.offset()
                if gpio == RECORD_BUTTON:
                    self._button(line, gpio, BUTTON_DEBOUNCE_S, self._record_pressed)
                elif gpio == POWER_BUTTON:
                    self._button(line, gpio, POWER_BUTTON_MIN_S, self._power_pressed)

    def _button(self, line, gpio, hold_s, action):
        now = time.monotonic()
        if now - self._last_press[gpio] < BUTTON_LOCKOUT_S:
            return
        time.sleep(hold_s)
        if line.get_value() != 0:
            return   # a bounce or a glitch
        self._last_press[gpio] = time.monotonic()
        action()

    def _record_pressed(self):
        self.presses["record"] += 1
        if self.app.shutdown_reason:
            return
        if self.action:
            log.info("record button: ignored, still %s", self.action)
            return
        threading.Thread(target=self._toggle_recording, daemon=True, name="record-button").start()

    def _toggle_recording(self):
        app = self.app
        try:
            if app.sessions.active:
                self.action = "stopping"
                log.info("record button: stopping the recording")
                app.sessions.stop()
            elif app.camera.status()["recording"]:
                self.action = "stopping"
                log.info("record button: stopping the Device-page video")
                app.camera.command({"cmd": "record_stop"}, timeout=60)
            else:
                self.action = "starting"
                log.info("record button: starting a recording")
                app.sessions.start()
        except Exception as e:
            log.warning("record button: %s", e)
            self.failed_until = time.monotonic() + 2
        finally:
            self.action = None

    def _power_pressed(self):
        self.presses["power"] += 1
        log.warning("power button pressed")
        self.app.shutdown("power button")

    def _read_power(self, now):
        if self.lines is None:
            return
        level = self.lines.get_values()[2]
        self.external_power = level == 0
        if level == 0:
            self.released_at = None
        elif self.released_at is None:
            self.released_at = now

    def on_battery_s(self, now=None):
        """Seconds without external power (after the glitch filter), or None."""
        if self.released_at is None:
            return None
        s = (now or time.monotonic()) - self.released_at
        return s if s >= UNPLUG_GLITCH_S else None

    def _check_unplugged(self, now):
        limit = self.settings["unplug_shutdown_s"]
        s = self.on_battery_s(now)
        if limit and s is not None and s >= limit:
            self.app.shutdown(f"no external power for {limit} s")

    def _update_outputs(self, now):
        app, out, st = self.app, self.outputs, self.settings
        # Record LED
        recording = bool(app.sessions.active) or bool(app.camera.status()["recording"])
        busy = self.action or app.sessions.busy
        if now < self.failed_until:
            out.drive("rec_led", 100 if _blink(8, now) else 0, "Flickering: the recording did not start")
        elif busy:
            out.drive("rec_led", 100 if _blink(3, now) else 0, f"Blinking: recording {busy}")
        else:
            out.drive("rec_led", 100 if recording else 0, "On: recording" if recording else "Off: not recording")
        # Battery LED
        soc = app.sampler.battery_soc()
        if soc is not None:
            if soc < st["battery_low_pct"]:
                self.battery_low = True
            elif soc >= st["battery_low_pct"] + BATTERY_HYSTERESIS_PCT:
                self.battery_low = False
        on_battery = self.on_battery_s(now)
        if on_battery is not None and st["unplug_shutdown_s"]:
            left = max(0, st["unplug_shutdown_s"] - on_battery)
            out.drive("bat_low_r", 100 if _blink(1, now) else 0,
                      f"Blinking: on battery, shutting down in {int(left // 60)}:{int(left % 60):02d}")
        else:
            low = f"below {st['battery_low_pct']} %"
            out.drive("bat_low_r", 100 if self.battery_low else 0,
                      f"On: battery {low}" if self.battery_low else
                      f"Off: battery {'unknown' if soc is None else f'{soc:.0f} %'} (on {low})")
        # Fan
        if now >= self._next_temp:
            self._next_temp = now + 1
            try:
                self.cpu_temp = int(THERMAL.read_text()) / 1000
            except (OSError, ValueError):
                self.cpu_temp = None
            if self.cpu_temp is not None:
                if self.cpu_temp >= st["fan_full_c"]:
                    self.fan_full = True
                elif self.cpu_temp <= st["fan_back_c"]:
                    self.fan_full = False
        temp = "unknown" if self.cpu_temp is None else f"{self.cpu_temp:.0f} °C"
        out.drive("fan", 100 if self.fan_full else st["fan_normal_pct"],
                  f"Full speed: CPU {temp} (back to normal at {st['fan_back_c']:g} °C)" if self.fan_full else
                  f"Normal {st['fan_normal_pct']} %: CPU {temp} (full speed from {st['fan_full_c']:g} °C)")
        # Power-on LED: the kernel's while the overlay is active; otherwise on while the app runs.
        out.drive("pwr_on_g", 100, "On: the Pi is running")

    # ----- status

    def status(self):
        now = time.monotonic()
        on_battery = self.on_battery_s(now)
        limit = self.settings["unplug_shutdown_s"]
        return {
            "inputs_ok": self.lines is not None, "input_error": self.input_error,
            "external_power": self.external_power,
            "on_battery_s": None if on_battery is None else round(on_battery),
            "shutdown_in_s": None if on_battery is None or not limit else max(0, round(limit - on_battery)),
            "cpu_temp_c": self.cpu_temp, "fan_full": self.fan_full, "battery_low": self.battery_low,
            "button_action": self.action, "presses": dict(self.presses),
            "shutting_down": self.app.shutdown_reason,
            "settings": dict(self.settings),
        }
