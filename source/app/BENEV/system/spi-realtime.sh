#!/bin/sh
# Run by benev-dashboard.service before the app starts (as root, ExecStartPre=+).
# Gives the kernel's SPI0 worker thread real-time priority, like the app's IMU reader thread:
# an SPI transfer preempted half-way makes the LSM6DSV16B drop the sample due at that moment.
for pid in $(pgrep -x spi0); do
    chrt -f -p 20 "$pid"
done
exit 0
