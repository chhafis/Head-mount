"""Drivers for the sensors wired to the Pi Zero 2W header.

  MAX17048   fuel gauge     I2C-1 (GPIO2/3) @ 0x36
  LSM6DSV16B 6-axis IMU     SPI0.0 (CE0 = GPIO8)
  MMC5983MA  magnetometer   SPI0.1 (CE1 = GPIO7)
"""
import logging
import math
import os
import struct
import time

import smbus2
import spidev

REALTIME_PRIORITY = 20   # within the service's LimitRTPRIO


class SensorError(Exception):
    pass


def make_thread_realtime(what):
    """Run the calling thread at SCHED_FIFO priority, so SPI transfers are not preempted.

    Measured 2026-10-24: when another task preempts an SPI transfer to the LSM6DSV16B half-way
    (chip select held, clock paused), the chip drops the sample due at that moment. Under full
    CPU load that was ~10 samples a minute at 960 Hz, and none at real-time priority.
    """
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(REALTIME_PRIORITY))   # 0 = this thread
        return True
    except (OSError, AttributeError) as e:
        logging.getLogger("dashboard").warning(
            "%s: no real-time priority (%s); expect a few IMU samples lost under heavy load", what, e)
        return False


def _wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise SensorError(f"timeout waiting for {what}")
        time.sleep(0.001)


class _SpiDevice:
    """Register access for chips that use bit 7 of the address byte as the read flag."""

    def __init__(self, bus, cs, mode, hz):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, cs)
        self.spi.mode = mode
        self.spi.max_speed_hz = hz

    def read(self, reg, n=1):
        return self.spi.xfer2([0x80 | reg] + [0] * n)[1:]

    def write(self, reg, value):
        self.spi.xfer2([reg & 0x7F, value])

    def close(self):
        self.spi.close()


class MAX17048:
    ADDR = 0x36
    VCELL, SOC, VERSION, CONFIG, CRATE, STATUS = 0x02, 0x04, 0x08, 0x0C, 0x16, 0x1A
    STATUS_FLAGS = (
        ("reset", 0x01),          # RI: powered up / reset, model not yet confirmed
        ("over_voltage", 0x02),   # VH
        ("under_voltage", 0x04),  # VL
        ("voltage_reset", 0x08),  # VR
        ("soc_low", 0x10),        # HD: SOC crossed the CONFIG.ATHD threshold
        ("soc_change", 0x20),     # SC: SOC changed by at least 1 %
    )

    def __init__(self, bus=1):
        self.bus = smbus2.SMBus(bus)
        try:
            self.version = self._word(self.VERSION)
        except Exception:
            self.bus.close()
            raise
        if self.version & 0xFFF0 != 0x0010:
            self.bus.close()
            raise SensorError(f"unexpected VERSION 0x{self.version:04X} at 0x{self.ADDR:02X}")
        self.ident = f"MAX17048 (VERSION 0x{self.version:04X})"

    def _word(self, reg):
        hi, lo = self.bus.read_i2c_block_data(self.ADDR, reg, 2)
        return hi << 8 | lo

    def read(self):
        crate = self._word(self.CRATE)
        if crate & 0x8000:
            crate -= 0x10000
        config = self._word(self.CONFIG)
        status = self._word(self.STATUS) >> 8
        vcell = self._word(self.VCELL) * 78.125e-6
        # A disturbed bus can "succeed" with all-zero bytes. RCOMP (CONFIG high byte) is never
        # written by us and the gauge runs from the cell itself, so neither can really read 0.
        if config >> 8 == 0 or not 2.0 < vcell < 5.0:
            raise SensorError(f"implausible reading (CONFIG 0x{config:04X}, VCELL {vcell:.3f} V): I2C data corrupted")
        return {
            "voltage": round(vcell, 4),                               # V
            "soc": round(self._word(self.SOC) / 256, 2),              # %
            "crate": round(crate * 0.208, 2),                         # %/hr, + = charging
            "alert": bool(config & 0x20),
            "alert_threshold": 32 - (config & 0x1F),                  # % SOC
            "flags": [name for name, bit in self.STATUS_FLAGS if status & bit],
        }

    def close(self):
        self.bus.close()


class LSM6DSV16B:
    """Accelerometer + gyroscope streamed through the chip's FIFO with hardware timestamps.

    Measured on this part (2026-10-24): the FIFO holds 256 words; every word carries a 2-bit
    counter shared by all words of one sampling event; accelerometer words are Z, Y, X like
    the output registers; at 240 Hz one sample is exactly 192 timestamp counts (so 48 at 960 Hz:
    both come from the same internal oscillator).
    """
    IF_CFG, WHO_AM_I, CTRL1, CTRL2, CTRL3, CTRL6, CTRL8, OUT_TEMP_L = 0x03, 0x0F, 0x10, 0x11, 0x12, 0x15, 0x17, 0x20
    FIFO_CTRL3, FIFO_CTRL4, FIFO_STATUS1, FIFO_DATA_OUT_TAG = 0x09, 0x0A, 0x1B, 0x78
    FUNCTIONS_ENABLE, INTERNAL_FREQ = 0x50, 0x4F
    TAG_GYRO, TAG_ACCEL, TAG_TIMESTAMP = 0x01, 0x02, 0x04
    IDS = {0x70: "LSM6DSV16X", 0x71: "LSM6DSV16B"}
    # Same code for the CTRL1/CTRL2 ODR and the FIFO batch rate. 960 Hz is the chip's "1 kHz".
    ODR_CODES = {240: 0x07, 480: 0x08, 960: 0x09}
    COUNTS_AT_240HZ = 192         # timestamp counts per sample at 240 Hz
    TS_EVERY = 8                  # a timestamp word every 8th sample (FIFO_CTRL4 DEC_TS = 8)
    READ_CHUNK_WORDS = 256        # FIFO words per SPI transfer
    GYRO_FS = {125: 0x0, 250: 0x1, 500: 0x2, 1000: 0x3, 2000: 0x4, 4000: 0xC}
    ACCEL_FS = {2: 0x0, 4: 0x1, 8: 0x2, 16: 0x3}

    def __init__(self, bus=0, cs=0, accel_g=4, gyro_dps=1000, odr_hz=960):
        odr = self.ODR_CODES[odr_hz]
        self.odr_hz = odr_hz
        self.COUNTS_PER_SAMPLE = self.COUNTS_AT_240HZ * 240 // odr_hz
        # 960 Hz moves ~14 KB/s of FIFO data; 8 MHz (the chip allows 10) keeps each read short.
        self.dev = _SpiDevice(bus, cs, mode=3, hz=8_000_000)
        try:
            who = self.dev.read(self.WHO_AM_I)[0]
            if who not in self.IDS:
                raise SensorError(f"unexpected WHO_AM_I 0x{who:02X}")
            # The DSV16B/16BX output block is Z, Y, X for the accelerometer (ST lsm6dsv16bx_reg.h);
            # the DSV16X is X, Y, Z.
            self.accel_zyx = who == 0x71
            self.ident = f"{self.IDS[who]} (WHO_AM_I 0x{who:02X})"

            self.dev.write(self.CTRL3, self.dev.read(self.CTRL3)[0] | 0x01)  # SW_RESET
            _wait_for(lambda: not self.dev.read(self.CTRL3)[0] & 0x01, 0.1, "IMU reset")
            self.dev.write(self.CTRL3, 0x44)                       # BDU + register auto-increment
            # I2C_I3C_disable. Left on, the chip's I2C side reacts to SPI traffic while its chip
            # select is high and now and then skips a sample (seen ~3 times a minute at 960 Hz).
            self.dev.write(self.IF_CFG, self.dev.read(self.IF_CFG)[0] | 0x01)
            self.dev.write(self.CTRL6, self.GYRO_FS[gyro_dps])
            self.dev.write(self.CTRL8, self.ACCEL_FS[accel_g])
            self.dev.write(self.CTRL1, odr)                        # high-performance mode
            self.dev.write(self.CTRL2, odr)
            self.dev.write(self.FUNCTIONS_ENABLE, self.dev.read(self.FUNCTIONS_ENABLE)[0] | 0x40)  # timestamps
            self.dev.write(self.FIFO_CTRL4, 0x00)                  # bypass: empties the FIFO
            self.dev.write(self.FIFO_CTRL3, odr << 4 | odr)
            self.dev.write(self.FIFO_CTRL4, 0x2 << 6 | 0x6)        # timestamp every 8th sample, stream mode
            freq_fine = struct.unpack("b", bytes(self.dev.read(self.INTERNAL_FREQ)))[0]
        except Exception:
            self.dev.close()
            raise
        self.accel_scale = 0.061e-3 * accel_g / 2      # g per LSB
        self.gyro_scale = 4.375e-3 * gyro_dps / 125    # dps per LSB
        # Starting guess for the timestamp period; motion.ClockMap refines it against the Pi clock.
        self.ns_per_count = 21750 / (1 + 0.0013 * freq_fine)
        self.config = {"accel_fs_g": accel_g, "gyro_fs_dps": gyro_dps, "odr_hz": odr_hz,
                       "fifo": "timestamped, every 8th sample"}
        self._ts_base = None      # unwrapped counter value of the last timestamp word
        self._since_ts = 0        # events since that timestamp word
        self._wraps = 0
        self._last_raw_ts = None
        self._cnt = None          # tag counter of the event being assembled
        self._event = {}
        self.overruns = 0
        # Data-quality counters: words with an unknown tag, sampling events missing from the
        # tag-counter sequence, events without both accelerometer and gyroscope data, and
        # timestamp words that show samples the chip never batched.
        self.errors = {"bad_tag": 0, "skipped": 0, "partial": 0, "ts_jump": 0}

    def read_fifo(self):
        """Drain the FIFO -> list of (counter, (ax, ay, az) g, (gx, gy, gz) dps), oldest first."""
        s1, s2 = self.dev.read(self.FIFO_STATUS1, 2)
        if s2 & 0x08:   # FIFO_OVR_LATCHED: samples were lost while nobody read
            self.overruns += 1
            self._ts_base = None   # restart timing at the next timestamp word
        words = (s2 & 0x01) << 8 | s1
        samples = []
        while words:
            n = min(words, self.READ_CHUNK_WORDS)
            raw = bytes(self.dev.read(self.FIFO_DATA_OUT_TAG, 7 * n))
            words -= n
            for i in range(0, 7 * n, 7):
                tag, cnt = raw[i] >> 3, (raw[i] >> 1) & 0x3
                if tag not in (self.TAG_ACCEL, self.TAG_GYRO, self.TAG_TIMESTAMP):
                    self.errors["bad_tag"] += 1
                    continue
                if cnt != self._cnt:
                    if self._cnt is not None and (cnt - self._cnt) % 4 != 1:
                        self.errors["skipped"] += (cnt - self._cnt - 1) % 4
                    self._finish_event(samples)
                    self._cnt = cnt
                if tag == self.TAG_TIMESTAMP:
                    ts = struct.unpack_from("<I", raw, i + 1)[0]
                    if self._last_raw_ts is not None and ts < self._last_raw_ts - (1 << 31):
                        self._wraps += 1
                    self._last_raw_ts = ts
                    self._event["ts"] = ts + (self._wraps << 32)
                elif tag in (self.TAG_ACCEL, self.TAG_GYRO):
                    self._event[tag] = struct.unpack_from("<3h", raw, i + 1)
        return samples

    def _finish_event(self, samples):
        event, self._event = self._event, {}
        if "ts" in event:
            if (self._ts_base is not None
                    and event["ts"] - self._ts_base != (self._since_ts + 1) * self.COUNTS_PER_SAMPLE):
                self.errors["ts_jump"] += 1   # the chip skipped samples between timestamp words
            self._ts_base, self._since_ts = event["ts"], 0
        elif self._ts_base is not None:
            self._since_ts += 1
        if self.TAG_ACCEL not in event or self.TAG_GYRO not in event:
            if event:
                self.errors["partial"] += 1
            return
        if self._ts_base is None:
            return   # before the first timestamp
        a = event[self.TAG_ACCEL]
        a = (a[2], a[1], a[0]) if self.accel_zyx else a
        samples.append((self._ts_base + self._since_ts * self.COUNTS_PER_SAMPLE,
                        tuple(v * self.accel_scale for v in a),
                        tuple(v * self.gyro_scale for v in event[self.TAG_GYRO])))

    def read_temp(self):
        return round(25 + struct.unpack("<h", bytes(self.dev.read(self.OUT_TEMP_L, 2)))[0] / 256, 2)

    def close(self):
        try:
            self.dev.write(self.FIFO_CTRL4, 0x00)
        finally:
            self.dev.close()


class MMC5983MA:
    STATUS, CTRL0, CTRL1, CTRL2, PRODUCT_ID = 0x08, 0x09, 0x0A, 0x0B, 0x2F
    TM_M, TM_T, SET, RESET = 0x01, 0x02, 0x08, 0x10
    MEAS_M_DONE, MEAS_T_DONE = 0x01, 0x02
    NULL = 1 << 17            # 18-bit output, zero field
    COUNTS_PER_GAUSS = 16384

    def __init__(self, bus=0, cs=1):
        self.dev = _SpiDevice(bus, cs, mode=0, hz=2_000_000)  # does not answer in SPI mode 3
        try:
            pid = self.dev.read(self.PRODUCT_ID)[0]
            if pid != 0x30:
                raise SensorError(f"unexpected PRODUCT_ID 0x{pid:02X}")
            self.ident = f"MMC5983MA (PRODUCT_ID 0x{pid:02X})"
            self.dev.write(self.CTRL1, 0x80)   # SW_RST
            time.sleep(0.015)
            self.dev.write(self.CTRL1, 0x00)   # BW 100 Hz: 8 ms per measurement, lowest noise
            self.dev.write(self.CTRL2, 0x00)   # one-shot mode
        except Exception:
            self.dev.close()
            raise

    def _measure(self, kind, done_bit):
        self.dev.write(self.STATUS, done_bit)  # write-1-to-clear the stale done flag
        self.dev.write(self.CTRL0, kind)
        _wait_for(lambda: self.dev.read(self.STATUS)[0] & done_bit, 0.05, "magnetometer measurement")

    def _measure_field(self, polarity):
        self.dev.write(self.CTRL0, polarity)   # SET/RESET pulse re-magnetises the sensing elements
        time.sleep(0.001)
        self._measure(self.TM_M, self.MEAS_M_DONE)
        b = self.dev.read(0x00, 7)
        return (
            (b[0] << 10 | b[1] << 2 | b[6] >> 6) - self.NULL,
            (b[2] << 10 | b[3] << 2 | (b[6] >> 4) & 0x3) - self.NULL,
            (b[4] << 10 | b[5] << 2 | (b[6] >> 2) & 0x3) - self.NULL,
        )

    def read(self):
        # Measuring after SET and after RESET flips the sign of the field but not of the bridge
        # offset, so half the difference is the offset-free field.
        s = self._measure_field(self.SET)
        r = self._measure_field(self.RESET)
        field = [(a - b) / 2 / self.COUNTS_PER_GAUSS * 100 for a, b in zip(s, r)]  # uT
        return {
            "field": [round(v, 3) for v in field],
            "field_norm": round(math.sqrt(sum(v * v for v in field)), 3),
            "offset": [round((a + b) / 2 / self.COUNTS_PER_GAUSS * 100, 3) for a, b in zip(s, r)],
        }

    def read_temp(self):
        self._measure(self.TM_T, self.MEAS_T_DONE)
        return round(self.dev.read(0x07)[0] * 200 / 255 - 75, 1)  # degC, 0 = -75, 255 = +125

    def close(self):
        self.dev.close()
