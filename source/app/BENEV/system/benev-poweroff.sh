#!/bin/sh
# Switch the whole board off at the end of a shutdown (installed as
# /usr/lib/systemd/system-shutdown/benev-poweroff).
#
# systemd-shutdown runs this as its very last step, after every file system is unmounted or
# read-only, with one argument: poweroff, halt, reboot or kexec. On a power-off (or halt) it pulls
# PI_KILL (GPIO5, through R92 to the LTC2954's KILL) low, and the LTC2954 (U13) then releases EN:
# all rails are off within about 30 us. On a reboot it does nothing, so the board stays on.
# On a board without U13 the KILL line goes nowhere, so this is harmless there.
case "$1" in
    poweroff|halt) ;;
    *) exit 0 ;;
esac
echo "benev: switching the board off (PI_KILL low)" > /dev/kmsg 2>/dev/null
for tool in /usr/bin/pinctrl /usr/bin/raspi-gpio; do
    if [ -x "$tool" ]; then
        exec "$tool" set 5 op dl
    fi
done
exit 0
