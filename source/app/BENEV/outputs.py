"""Fan and LED outputs on the BENEV HAT.

Every load is switched by a low-side AO3400A MOSFET, so GPIO high = on
(bring-up procedure section 12.6). GPIO12 (fan) and GPIO13 (LED2_B) are the Pi's
hardware PWM pins: with `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4`
active they get 0-100 % control, otherwise they are plain on/off like the rest.

Automatic outputs are driven by board.py (fan by temperature, LEDs by state) and cannot be
switched by hand. PWR_ON_G belongs to the kernel when `dtoverlay=gpio-led,gpio=23,
label=benev-power,trigger=default-on` is active: on from early boot until the very end of
shutdown, so it also shows when the Pi is safe to unplug.
"""
import json
import os
import threading
import time
from pathlib import Path

import gpiod

PWM_DEVICE = "3f20c000.pwm"   # BCM2710 PWM block that drives GPIO12/13 in ALT0
KERNEL_LEDS = Path("/sys/class/leds")

# name, label, GPIO, PWM channel, PWM frequency (Hz), schematic path, automatic
OUTPUTS = [
    ("fan", "Fan", 12, 0, 25_000, "FAN_CTRL · GPIO12 · Q3 → J10", True),
    ("pwr_on_g", "Power-on LED", 23, None, None, "PWR_ON_G · GPIO23 · Q1 → D8", True),
    ("rec_led", "Record LED", 24, None, None, "REC_LED · GPIO24 · Q4 → D9", True),
    ("bat_low_r", "Battery-low LED", 16, None, None, "BAT_LOW_R · GPIO16 · Q2 → D8", True),
    ("led2_b", "LED2 blue", 13, 1, 1_000, "LED2_B · GPIO13 · Q5 → D9", False),
    ("spare_led_g", "Spare green LED", 26, None, None, "SPARE_LED_G · GPIO26 · Q6 → D9", False),
]
KERNEL_LED_LABELS = {"pwr_on_g": "benev-power"}


def find_pwm_chip():
    for chip in Path("/sys/class/pwm").glob("pwmchip*"):
        if os.path.realpath(chip / "device").endswith(PWM_DEVICE):
            return chip
    return None


class GpioOutput:
    dimmable = False
    kernel = False

    def __init__(self, chip, gpio):
        self.line = chip.get_line(gpio)
        self.line.request(consumer="benev-dashboard", type=gpiod.LINE_REQ_DIR_OUT, default_val=0)

    def set(self, level):
        self.line.set_value(1 if level > 0 else 0)


class PwmOutput:
    dimmable = True
    kernel = False

    def __init__(self, chip_dir, channel, hz):
        self.dir = chip_dir / f"pwm{channel}"
        if not self.dir.exists():
            (chip_dir / "export").write_text(str(channel))
        # udev hands the new pwmN files to the gpio group a moment after export
        deadline = time.monotonic() + 2
        while not os.access(self.dir / "duty_cycle", os.W_OK):
            if time.monotonic() > deadline:
                raise PermissionError(f"{self.dir}/duty_cycle is not writable")
            time.sleep(0.05)
        self.period = round(1e9 / hz)
        self._write("duty_cycle", 0)   # duty must never exceed the period, so zero it first
        self._write("period", self.period)
        self._write("enable", 1)

    def _write(self, name, value):
        (self.dir / name).write_text(str(value))

    def set(self, level):
        self._write("duty_cycle", self.period * level // 100)


class KernelLed:
    """An LED the kernel drives (gpio-led overlay); read-only here."""
    dimmable = False
    kernel = True

    def __init__(self, path):
        self.path = path

    def level(self):
        try:
            return 100 if int((self.path / "brightness").read_text()) > 0 else 0
        except (OSError, ValueError):
            return 0

    def set(self, level):
        raise RuntimeError("controlled by the kernel (gpio-led overlay)")


class Outputs:
    """All outputs; levels are 0-100. Manual levels survive restarts via a small JSON file."""

    def __init__(self, state_file):
        self.state_file = Path(state_file)
        self.lock = threading.Lock()
        self.rev = 0
        self.closed = False   # after off(): nothing switches back on
        try:
            saved = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            saved = {}
        pwm_chip = find_pwm_chip()
        self.chip = gpiod.Chip("gpiochip0")
        self.items = {}
        for name, label, gpio, channel, hz, detail, auto in OUTPUTS:
            item = {"name": name, "label": label, "gpio": gpio, "detail": detail,
                    "pwm_capable": channel is not None, "dimmable": False, "level": 0,
                    "auto": auto, "kernel": False, "note": "", "error": None, "driver": None}
            try:
                led = KERNEL_LEDS / KERNEL_LED_LABELS.get(name, "-")
                if led.is_dir():
                    item["driver"] = KernelLed(led)
                    item["kernel"] = True
                    item["level"] = item["driver"].level()
                elif channel is not None and pwm_chip is not None:
                    item["driver"] = PwmOutput(pwm_chip, channel, hz)
                else:
                    item["driver"] = GpioOutput(self.chip, gpio)
                item["dimmable"] = item["driver"].dimmable
                if not item["kernel"]:
                    item["level"] = 0 if auto else self._clamp(saved.get(name, 0), item["dimmable"])
                    item["driver"].set(item["level"])
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"
            self.items[name] = item

    @staticmethod
    def _clamp(level, dimmable):
        level = max(0, min(100, int(level)))
        return level if dimmable else (100 if level else 0)

    def set(self, name, level):
        """A manual change from the Device page."""
        item = self.items[name]   # KeyError -> unknown output
        if item["auto"]:
            raise RuntimeError(f"{item['label']} is controlled automatically")
        if item["driver"] is None:
            raise RuntimeError(item["error"])
        if isinstance(level, bool):
            level = 100 if level else 0
        if not isinstance(level, (int, float)):
            raise ValueError("level must be a number 0-100")
        with self.lock:
            if self.closed:
                raise RuntimeError("the dashboard is stopping")
            self._apply(item, level)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({n: i["level"] for n, i in self.items.items() if not i["auto"]}))
            os.replace(tmp, self.state_file)

    def drive(self, name, level, note=None):
        """An automatic change (board.py); only touches the hardware when something changed."""
        item = self.items[name]
        with self.lock:
            if note is not None and note != item["note"]:
                item["note"] = note
                self.rev += 1
            if item["driver"] is None or item["kernel"] or self.closed:
                return
            if self._clamp(level, item["dimmable"]) != item["level"]:
                self._apply(item, level)

    def _apply(self, item, level):
        level = self._clamp(level, item["dimmable"])
        item["driver"].set(level)
        item["level"] = level
        self.rev += 1

    def off(self):
        """Everything off (service stop): the board stays powered after the Pi halts."""
        with self.lock:
            self.closed = True
            for item in self.items.values():
                if item["driver"] is not None and not item["kernel"]:
                    try:
                        item["driver"].set(0)
                        item["level"] = 0
                    except Exception:
                        pass

    def state(self):
        with self.lock:
            for item in self.items.values():
                if item["kernel"]:
                    item["level"] = item["driver"].level()
            return [{k: v for k, v in item.items() if k != "driver"} for item in self.items.values()]
