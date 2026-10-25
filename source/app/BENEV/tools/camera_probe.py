"""Check whether the AR0234 on the camera board answers on the Pi's camera I2C bus.

Releases the sensor reset (GPIO40 = cam1_reg enable -> J6 pin 17 CAM_RESET_3V3),
scans i2c-10 (GPIO44/45) and reads the chip ID, then puts GPIO40 back to low.

    sudo dtparam i2c_vc=on     # once per boot, creates /dev/i2c-10 (never remove it again at runtime)
    python3 ~/BENEV/tools/camera_probe.py [--hold]   # --hold leaves reset released for scoping
"""
import subprocess
import sys
import time

from smbus2 import SMBus, i2c_msg

BUS = 10
ADDRESSES = (0x10, 0x18)   # SADDR low / high


def pinctrl(*args):
    return subprocess.run(["pinctrl", *args], capture_output=True, text=True).stdout.strip()


def read16(bus, addr, reg):
    w, r = i2c_msg.write(addr, [reg >> 8, reg & 0xFF]), i2c_msg.read(addr, 2)
    bus.i2c_rdwr(w, r)
    hi, lo = list(r)
    return hi << 8 | lo


def main():
    hold = "--hold" in sys.argv
    try:
        bus = SMBus(BUS)
    except FileNotFoundError:
        sys.exit(f"/dev/i2c-{BUS} missing: run 'sudo dtparam i2c_vc=on' first")
    try:
        pinctrl("set", "40", "op", "dh")
        time.sleep(0.05)   # > 150000 EXTCLK cycles at 24 MHz for internal init
        print("reset released:", pinctrl("get", "40"))
        found = []
        for a in range(0x08, 0x78):
            try:
                bus.read_byte(a)
                found.append(f"0x{a:02X}")
            except OSError:
                pass
        print(f"devices on i2c-{BUS}:", found or "none")
        for addr in ADDRESSES:
            try:
                chip = read16(bus, addr, 0x3000)
                print(f"0x{addr:02X}: chip_version_reg R0x3000 = 0x{chip:04X}"
                      f" ({'AR0234' if chip == 0x0A56 else 'unexpected'}), R0x300E = 0x{read16(bus, addr, 0x300E):04X}")
            except OSError as e:
                print(f"0x{addr:02X}: no answer ({e.strerror})")
        if hold:
            input("reset held released; press Enter to put GPIO40 back low ")
    finally:
        pinctrl("set", "40", "op", "dl")
        print("reset asserted again:", pinctrl("get", "40"))


if __name__ == "__main__":
    main()
