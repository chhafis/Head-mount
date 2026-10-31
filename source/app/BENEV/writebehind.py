"""Write-behind: run slow file writes on a thread of their own.

The SD card of a Pi Zero 2 W stalls now and then for a few hundred milliseconds (measured:
240-580 ms). A thread that writes to it directly stops for as long, and for a recording that is
costly in two places:

  * the video encoder's polling thread (picamera2) writes each frame to the file and also hands
    the camera its buffers back; while it waits for the card, the camera runs out of buffers and
    drops whole frames;
  * the IMU reader thread must empty the sensor's 256-word FIFO (about 63 ms at 1920 Hz); while it
    waits for the card the FIFO overflows and samples are lost.

The caller puts a job (a function and its arguments) into the queue and goes on; the jobs run in
order on the writer thread. The queue is limited by size so a card that stops for good cannot use
all the memory; what happens then is up to the caller (put() tells it).
"""
import collections
import logging
import threading
import time

log = logging.getLogger("dashboard")


class WriteBehind:
    def __init__(self, name, max_bytes=64 << 20):
        self.name = name
        self.max_bytes = max_bytes
        self._jobs = collections.deque()
        self._cond = threading.Condition()
        self._closing = False
        self._thread = None
        self.queued_bytes = 0
        self.max_queued_bytes = 0
        self.max_delay_s = 0.0        # longest time a job waited in the queue plus its own run time
        self.max_job_s = 0.0          # longest single job (a stall of the disk shows up here)
        self.jobs_done = 0
        self.errors = 0
        self.last_error = None

    def start(self):
        self._closing = False
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    @property
    def full(self):
        return self.queued_bytes >= self.max_bytes

    def put(self, fn, *args, size=0):
        """Queue fn(*args); `size` is what it holds in memory. Never blocks."""
        with self._cond:
            self._jobs.append((time.monotonic(), size, fn, args))
            self.queued_bytes += size
            if self.queued_bytes > self.max_queued_bytes:
                self.max_queued_bytes = self.queued_bytes
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._jobs and not self._closing:
                    self._cond.wait()
                if not self._jobs:
                    return
                queued_at, size, fn, args = self._jobs.popleft()
                self.queued_bytes -= size
            t0 = time.monotonic()
            try:
                fn(*args)
            except Exception as e:   # keep writing the rest: one bad row must not stop a recording
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.errors <= 3:
                    log.warning("%s: %s", self.name, self.last_error)
            done = time.monotonic()
            self.jobs_done += 1
            self.max_job_s = max(self.max_job_s, done - t0)
            self.max_delay_s = max(self.max_delay_s, done - queued_at)

    def close(self, timeout=120.0):
        """Run what is queued, then stop the thread. -> the statistics."""
        with self._cond:
            self._closing = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout)
        return self.stats()

    def stats(self):
        return {"max_queue_mb": round(self.max_queued_bytes / 1e6, 2),
                "max_delay_ms": round(self.max_delay_s * 1000),
                "longest_write_ms": round(self.max_job_s * 1000),
                "errors": self.errors}
