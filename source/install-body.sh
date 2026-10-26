#!/bin/bash
# BENEV dashboard installer for a fresh Raspberry Pi Zero 2 W (Raspberry Pi OS Bookworm).
#
# Sets up the same software as the original BENEV board:
#   - I2C, SPI and hardware PWM (fan on GPIO12, LED2 on GPIO13)
#   - Kurokesu AR0234 camera driver (DKMS) and Kurokesu's libcamera 0.7.2 with AR0234 support
#     (built from source into /usr/local, next to the system libcamera)
#   - the BENEV recorder web app as the systemd service "benev-dashboard" on ports 80 and 8080,
#     at http://benev.local (host name "benev", advertised over mDNS / DNS-SD)
#   - the Wi-Fi manager "benev-wifi": joins saved Wi-Fi, otherwise starts the open "Benev AI"
#     hotspot (10.42.0.1) whose captive portal is the Wi-Fi setup page
#   - status LEDs, fan control, record and power buttons (board.py; power LED in config.txt)
#   - over-the-air updates from GitHub Releases (benev-update, /etc/benev/ota.json)
#
# Usage, on the Pi, as your normal user:
#   sudo bash benev-install.sh              full install (about 1 hour; asks to reboot at the end)
#   sudo bash benev-install.sh --app-only   only update the app, Wi-Fi manager, updater, services
#   sudo bash benev-install.sh --update     what OTA updates run: --app-only plus missing
#                                           packages and boot settings (no system upgrade)
# Options: --yes (no questions, reboot at the end)  --no-reboot  --no-upgrade  --user NAME
#          --hostname NAME (default: benev on a new Pi, an existing name is kept)
#          --ota-repo OWNER/NAME (update source)  --force
#
# Safe to run again: every step checks whether it is already done. Log: /var/log/benev-install.log
set -Eeuo pipefail

# Pinned versions: exactly what runs on the original board (tested 2026-10-23).
DRIVER_REPO=https://github.com/Kurokesu/ar0234-rpi-driver.git
DRIVER_TAG=v0.1.2
DRIVER_COMMIT=eefdf9c42cb8f06f7cf00a18b08c7c23d9363a9f
DRIVER_DKMS=ar0234-rpi-dkms/0.1.2
LIBCAMERA_REPO=https://github.com/Kurokesu/libcamera.git
LIBCAMERA_COMMIT=409deedcfc9017e405c18abdacd632fd8372fccf
LIBCAMERA_VERSION=0.7.2
PORTS="80 8080"          # 80 so http://benev.local works without a port; 8080 as before
CONFIG=/boot/firmware/config.txt
LOG=/var/log/benev-install.log
SRC_DIR=/usr/local/src
LIB_DIR=/usr/local/lib/benev

STATE_DIR=/var/lib/benev
# Needed at run time (also checked by --update); the build tools are only for a full install.
RUNTIME_PKGS="python3-smbus2 python3-spidev python3-libgpiod python3-numpy python3-simplejpeg
    python3-picamera2 python3-av python3-pidng python3-piexif python3-pil
    i2c-tools v4l-utils raspi-utils
    network-manager dnsmasq-base wpasupplicant rfkill avahi-daemon openssh-client"
BUILD_PKGS="git dkms build-essential
    meson ninja-build pkg-config cmake libyaml-dev python3-yaml python3-ply python3-jinja2
    libgnutls28-dev openssl pybind11-dev python3-dev libboost-dev libudev-dev libtiff-dev"

APP_ONLY=0 UPDATE=0 ASSUME_YES=0 NO_REBOOT=0 NO_UPGRADE=0 FORCE=0 TARGET_USER="" HOST_NAME="" OTA_REPO=""
usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --app-only) APP_ONLY=1 ;;
        --update) UPDATE=1 ;;
        --ota-repo) OTA_REPO="${2:?--ota-repo needs OWNER/NAME}"; shift ;;
        --yes|-y) ASSUME_YES=1 ;;
        --no-reboot) NO_REBOOT=1 ;;
        --no-upgrade) NO_UPGRADE=1 ;;
        --force) FORCE=1 ;;
        --user) TARGET_USER="${2:?--user needs a name}"; shift ;;
        --hostname) HOST_NAME="${2:?--hostname needs a name}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 2 ;;
    esac
    shift
done

die() { echo; echo "!! $*"; exit 1; }
ask() {   # ask "question" -> 0 for yes
    [[ $ASSUME_YES == 1 ]] && return 0
    [[ -t 0 ]] || return 1
    local reply; read -r -p "$1 [y/N] " reply; [[ $reply =~ ^[Yy] ]]
}

# --- preconditions ---------------------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "Run this with sudo:  sudo bash $0"
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
[[ -n $TARGET_USER && $TARGET_USER != root ]] || die "Run it with sudo from your normal user account, or pass --user NAME."
id "$TARGET_USER" >/dev/null 2>&1 || die "User '$TARGET_USER' does not exist."
[[ -z $HOST_NAME || $HOST_NAME =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || die "Invalid host name '$HOST_NAME' (letters, digits and '-')."
[[ -z $OTA_REPO || $OTA_REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--ota-repo must look like OWNER/NAME."
HOME_DIR=$(getent passwd "$TARGET_USER" | cut -d: -f6)
APP_DIR="$HOME_DIR/BENEV"

mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "=== BENEV installer started $(date) for user $TARGET_USER ==="

STEP=0
trap 'echo; echo "!! Failed in step $STEP at line $LINENO. Fix the problem and run the installer again (it continues where it can). Log: $LOG"' ERR
step() { STEP=$((STEP + 1)); echo; echo "==> [$STEP] $*"; }

MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
. /etc/os-release
ARCH=$(dpkg --print-architecture)
case "$ARCH" in
    armhf) TRIPLET=arm-linux-gnueabihf ;;
    arm64) TRIPLET=aarch64-linux-gnu ;;
    *) die "Unsupported architecture $ARCH." ;;
esac
echo "Board: $MODEL | OS: $PRETTY_NAME ($ARCH) | kernel $(uname -r)"
if [[ $MODEL != *"Zero 2"* ]]; then
    echo "Warning: this was made for a Raspberry Pi Zero 2 W, not '$MODEL'."
    [[ $FORCE == 1 ]] || ask "Continue anyway?" || die "Stopped."
fi
if [[ ${VERSION_CODENAME:-} != bookworm ]]; then
    # The dashboard uses libgpiod v1 and dphys-swapfile, which Trixie changed.
    [[ $FORCE == 1 ]] || die "Needs Raspberry Pi OS Bookworm (found ${VERSION_CODENAME:-unknown}). Use --force to try anyway."
fi

PAYLOAD=$(mktemp -d)
SWAP_ORIG="" SWAP_CHANGED=0
cleanup() {
    rm -rf "$PAYLOAD"
    if [[ $SWAP_CHANGED == 1 ]]; then
        sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=$SWAP_ORIG/" /etc/dphys-swapfile
        systemctl restart dphys-swapfile || true
        echo "Swap restored to $SWAP_ORIG MB."
    fi
}
trap cleanup EXIT

# The dashboard files travel inside this script, base64-encoded after the marker line.
line=$(awk '/^__BENEV_PAYLOAD_BELOW__$/ {print NR + 1; exit}' "$0")
[[ -n $line ]] || die "The installer file is incomplete (no payload). Copy it again without editing it."
tail -n +"$line" "$0" | base64 -d | tar -xz -C "$PAYLOAD"

# --- steps -----------------------------------------------------------------------------------

install_app() {
    step "Installing the dashboard into $APP_DIR"
    mkdir -p "$APP_DIR/media"
    # Copies code only; photos, recordings and settings (camera.json, outputs.json) are kept.
    cp -r "$PAYLOAD/BENEV/." "$APP_DIR/"
    chown -R "$TARGET_USER:$TARGET_USER" "$APP_DIR"
    echo "Dashboard version: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"
}

install_service() {
    step "Setting up the benev-dashboard service, permissions and the shutdown button"
    # netdev: talk to the Wi-Fi manager's socket
    usermod -aG i2c,spi,gpio,video,netdev "$TARGET_USER"
    local port_args="" p
    for p in $PORTS; do port_args+=" --port $p"; done
    install -d -m 755 "$LIB_DIR"
    install -m 755 "$PAYLOAD/BENEV/system/spi-realtime.sh" "$LIB_DIR/spi-realtime.sh"
    cat > /etc/systemd/system/benev-dashboard.service <<EOF
[Unit]
Description=BENEV recorder web app (http://benev.local)
After=network.target benev-wifi.service

[Service]
User=$TARGET_USER
Group=$TARGET_USER
WorkingDirectory=$APP_DIR
# As root: real-time priority for the kernel's SPI thread (IMU timing)
ExecStartPre=+/bin/sh $LIB_DIR/spi-realtime.sh
ExecStart=/usr/bin/python3 $APP_DIR/server.py$port_args
# Port 80 without root; real-time priority (up to 20) for the IMU and magnetometer threads
AmbientCapabilities=CAP_NET_BIND_SERVICE
LimitRTPRIO=20
# On stop only the app gets SIGTERM: it saves a running recording, then ends the camera worker.
KillMode=mixed
TimeoutStopSec=45
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    cp /etc/systemd/system/benev-dashboard.service "$APP_DIR/benev-dashboard.service"
    chown "$TARGET_USER:$TARGET_USER" "$APP_DIR/benev-dashboard.service"
    # The app may run exactly these as root: power off / restart (buttons, unplugged on battery)
    # and start the updater's check or install units. Nothing else.
    local rule=/etc/sudoers.d/benev-dashboard tmp
    tmp=$(mktemp)
    printf '%s ALL=(root) NOPASSWD: %s\n' "$TARGET_USER" \
        "/usr/bin/systemctl poweroff, /usr/bin/systemctl reboot, /usr/bin/systemctl start --no-block benev-update@check.service, /usr/bin/systemctl start --no-block benev-update@install.service" > "$tmp"
    visudo -cqf "$tmp" || die "Generated sudoers rule failed validation."
    install -m 440 "$tmp" "$rule"
    rm -f "$tmp"
    systemctl daemon-reload
    systemctl enable benev-dashboard.service
    systemctl restart benev-dashboard.service
    sleep 3
    systemctl is-active --quiet benev-dashboard.service || die "The dashboard service did not start: journalctl -u benev-dashboard"
    echo "Service running."
}

# The host name is the device's name (name.local) and can be changed on the Device page, so
# the installer, which also runs for every update, only sets it when asked (--hostname NAME) or
# on a Pi that still has the Raspberry Pi OS default name.
set_hostname() {
    local current wanted
    current=$(hostname)
    if [[ -n $HOST_NAME ]]; then
        wanted=$HOST_NAME
    elif [[ ${current,,} == raspberrypi ]]; then
        wanted=benev
    else
        echo "Host name stays $current: http://${current,,}.local (rename it on the Device page)"
        return
    fi
    HOST_NAME=$wanted
    if [[ ${current,,} == "${HOST_NAME,,}" ]]; then
        echo "Host name is $current: http://${current,,}.local"
        return
    fi
    # /etc/hosts first, so sudo can still resolve the new name.
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOST_NAME/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$HOST_NAME" >> /etc/hosts
    fi
    hostnamectl set-hostname "$HOST_NAME"
    systemctl restart avahi-daemon || true
    echo "Host name changed from $current to $HOST_NAME: http://$HOST_NAME.local"
}

install_network() {
    step "Setting up Wi-Fi (saved networks, otherwise the setup hotspot) and the .local name"
    set_hostname
    local sys="$PAYLOAD/BENEV/system"
    install -d -m 755 /etc/avahi/services
    install -m 644 "$sys/benev.avahi.service" /etc/avahi/services/benev.service
    systemctl enable avahi-daemon >/dev/null 2>&1 || true
    systemctl reload-or-restart avahi-daemon || echo "Warning: avahi-daemon is not running; benev.local will not resolve."
    if ! systemctl is-active --quiet NetworkManager; then
        echo "NetworkManager is not running, so the Wi-Fi manager and hotspot are skipped."
        return
    fi
    install -d -m 755 "$LIB_DIR" /etc/NetworkManager/dnsmasq-shared.d
    install -m 755 "$sys/wifi_manager.py" "$LIB_DIR/wifi_manager.py"
    install -m 644 "$sys/benev-captive.conf" /etc/NetworkManager/dnsmasq-shared.d/benev-captive.conf
    install -m 644 "$sys/benev-wifi.service" /etc/systemd/system/benev-wifi.service
    if [[ ! -e /etc/benev/wifi.json ]]; then
        # Hotspot settings; an empty password means an open hotspot.
        install -d -m 755 /etc/benev
        (umask 077; printf '{\n  "hotspot_ssid": "Benev AI",\n  "hotspot_password": ""\n}\n' > /etc/benev/wifi.json)
    fi
    systemctl daemon-reload
    systemctl enable benev-wifi.service
    # Safe over SSH: while a saved network is connected the manager changes nothing.
    systemctl restart benev-wifi.service
    sleep 2
    systemctl is-active --quiet benev-wifi.service || die "The Wi-Fi manager did not start: journalctl -u benev-wifi"
    echo "Wi-Fi manager running. Hotspot settings: /etc/benev/wifi.json"
}

update_system() {
    step "Updating Raspberry Pi OS (this can take 15-30 minutes on a Zero 2 W)"
    apt-get update
    if [[ $NO_UPGRADE == 1 ]]; then
        echo "Skipped (--no-upgrade)."
        return
    fi
    DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold full-upgrade
    # The camera driver is compiled for the running kernel, so a kernel update needs a reboot first.
    local running flavour newest
    running=$(uname -r)
    flavour=${running##*+rpt-}
    newest=$( (ls -1 /lib/modules | grep -- "-${flavour}\$" || true) | sort -V | tail -1)
    if [[ -n $newest && $newest != "$running" ]]; then
        echo
        echo "The update installed a new kernel ($newest; running $running)."
        echo "Reboot, then run this installer again; it continues from here."
        if [[ $NO_REBOOT == 0 ]] && ask "Reboot now?"; then reboot; fi
        exit 0
    fi
}

install_packages() {
    step "Installing packages"
    local headers="linux-headers-$(uname -r)"
    apt-cache show "$headers" >/dev/null 2>&1 || die "Kernel headers $headers are not available. Run: sudo apt update && sudo apt full-upgrade -y && sudo reboot, then this installer again."
    # shellcheck disable=SC2086  # word splitting of the package lists is intended
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        $RUNTIME_PKGS $BUILD_PKGS "$headers"
}

install_runtime_packages() {
    step "Checking the packages the app needs"
    local missing="" p
    for p in $RUNTIME_PKGS; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=" $p"
    done
    if [[ -z $missing ]]; then
        echo "All installed."
        return
    fi
    echo "Installing:$missing"
    apt-get update
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
}

install_ota() {
    step "Setting up over-the-air updates from GitHub (benev-update)"
    local sys="$PAYLOAD/BENEV/system" repo=""
    install -d -m 755 "$LIB_DIR" /etc/benev "$STATE_DIR/ota"
    install -m 755 "$sys/ota.py" "$LIB_DIR/ota.py"
    ln -sf "$LIB_DIR/ota.py" /usr/local/bin/benev-update
    install -m 644 "$sys/benev-update@.service" /etc/systemd/system/benev-update@.service
    install -m 644 "$sys/benev-update.timer" /etc/systemd/system/benev-update.timer
    # Update source: --ota-repo, else what the release build baked in, else keep the current one.
    # Update source: --ota-repo; else the one already configured; else what a release build
    # baked in. Only root can change it (the web app cannot): it decides what code runs as root.
    [[ -f $sys/ota-repo ]] && repo=$(tr -d '[:space:]' < "$sys/ota-repo")
    python3 - "$OTA_REPO" "$repo" "$TARGET_USER" <<'PY'
import json, os, sys
path, forced, baked, user = "/etc/benev/ota.json", sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
cfg = {"repo": "", "token": "", "auto_install": False, **cfg, "user": user}
if forced:
    cfg["repo"] = forced
elif not cfg["repo"] and baked:
    cfg["repo"] = baked
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
print("Update source:", cfg["repo"] or "not set (add \"repo\": \"owner/name\" to " + path + ")")
PY
    if [[ -f $sys/ota_allowed_signers ]]; then
        install -m 644 "$sys/ota_allowed_signers" /etc/benev/ota_allowed_signers
        echo "Releases must be signed (keys in /etc/benev/ota_allowed_signers)."
    fi
    systemctl daemon-reload
    systemctl enable --now benev-update.timer
}

enable_interfaces() {
    step "Enabling I2C and SPI"
    raspi-config nonint do_i2c 0
    raspi-config nonint do_spi 0
}

install_driver() {
    step "Installing the Kurokesu AR0234 camera driver ($DRIVER_TAG)"
    if dkms status "$DRIVER_DKMS" -k "$(uname -r)" 2>/dev/null | grep -q installed \
            && [[ -e /boot/firmware/overlays/ar0234.dtbo ]]; then
        echo "Already installed for kernel $(uname -r)."
        return
    fi
    local src="$SRC_DIR/ar0234-rpi-driver"
    rm -rf "$src"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$DRIVER_TAG" "$DRIVER_REPO" "$src"
    [[ $(git -C "$src" rev-parse HEAD) == "$DRIVER_COMMIT" ]] \
        || die "The driver tag $DRIVER_TAG no longer points at the tested commit $DRIVER_COMMIT."
    (cd "$src" && ./setup.sh)
    [[ -e /boot/firmware/overlays/ar0234.dtbo ]] || die "Driver installed but ar0234.dtbo is missing."
}

grow_swap() {
    [[ -f /etc/dphys-swapfile ]] || { echo "No dphys-swapfile; building with less memory headroom."; return; }
    SWAP_ORIG=$(sed -n 's/^CONF_SWAPSIZE=\([0-9]*\).*/\1/p' /etc/dphys-swapfile)
    if [[ -n $SWAP_ORIG && $SWAP_ORIG -lt $1 ]]; then
        sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=$1/" /etc/dphys-swapfile
        systemctl restart dphys-swapfile
        SWAP_CHANGED=1
        echo "Swap raised from $SWAP_ORIG to $1 MB for the build (restored afterwards)."
    fi
}

build_libcamera() {
    step "Building Kurokesu libcamera $LIBCAMERA_VERSION with AR0234 support (about 45 minutes)"
    if [[ -e /usr/local/lib/$TRIPLET/libcamera.so.$LIBCAMERA_VERSION \
            && -e /usr/local/share/libcamera/ipa/rpi/vc4/ar0234.json \
            && -d /usr/local/lib/python3/dist-packages/libcamera ]]; then
        echo "Already installed in /usr/local."
        return
    fi
    local src="$SRC_DIR/libcamera-kurokesu"
    if [[ ! -d $src/.git ]]; then
        rm -rf "$src"
        git init -q "$src"
        git -C "$src" remote add origin "$LIBCAMERA_REPO"
    fi
    git -C "$src" fetch -q --depth 1 origin "$LIBCAMERA_COMMIT"
    git -C "$src" checkout -q --force FETCH_HEAD
    grow_swap 1024
    rm -rf "$src/build"
    (cd "$src" && meson setup build --buildtype=release --prefix=/usr/local \
        -Dpipelines=rpi/vc4 -Dipas=rpi/vc4 -Dv4l2=disabled -Dgstreamer=disabled -Dtest=false \
        -Dlc-compliance=disabled -Dcam=disabled -Dqcam=disabled -Ddocumentation=disabled -Dpycamera=enabled)
    nice -n 10 ninja -C "$src/build" -j2
    ninja -C "$src/build" install
    ldconfig
    echo "Installed. Source and build kept in $src (uninstall: sudo ninja -C $src/build uninstall)."
}

add_config_line() {   # add_config_line "line" "regex that means it is already there"
    if grep -Eq "$2" "$CONFIG"; then
        echo "  already set: $1"
        return
    fi
    # Our lines must apply to every board model, so make sure they land in an [all] section.
    if [[ $(awk '/^\[/ {s = $0} END {print s}' "$CONFIG") != "[all]" ]] && grep -q '^\[' "$CONFIG"; then
        printf '\n[all]\n' >> "$CONFIG"
    fi
    echo "$1" >> "$CONFIG"
    echo "  added: $1"
    BOOT_CHANGED=1
}

configure_boot() {
    step "Configuring $CONFIG (camera overlay, PWM, power LED)"
    BOOT_CHANGED=0
    local backup="$CONFIG.bak-benev-$(date +%Y%m%d-%H%M%S)"
    cp "$CONFIG" "$backup"
    if grep -q '^camera_auto_detect=1' "$CONFIG"; then
        sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
        echo "  set: camera_auto_detect=0"
        BOOT_CHANGED=1
    fi
    add_config_line "camera_auto_detect=0" '^camera_auto_detect=0'
    add_config_line "dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4" '^dtoverlay=pwm-2chan'
    add_config_line "dtoverlay=ar0234" '^dtoverlay=ar0234(,|[[:space:]]*$)'
    # PWR_ON_G (GPIO23): lit by the firmware about a second after power-on, then kept on by the
    # kernel, which switches it off at the very end of shutdown (= safe to unplug).
    add_config_line "gpio=23=op,dh" '^gpio=23='
    add_config_line "dtoverlay=gpio-led,gpio=23,label=benev-power,trigger=default-on" '^dtoverlay=gpio-led,gpio=23(,|$)'
    if [[ $BOOT_CHANGED == 1 ]]; then
        echo "  backup of the previous file: $backup"
        touch /run/benev-reboot-required   # tmpfs: the app shows it until the next boot
    else
        rm -f "$backup"
    fi
}

# Quick modes (--update, --app-only; OTA updates run --update) trust that a full install was done
# before. Say so when the camera software is missing, instead of ending with a plain "Done".
check_camera_stack() {
    local missing=""
    [[ -e /usr/local/lib/$TRIPLET/libcamera.so.$LIBCAMERA_VERSION ]] || missing+=" libcamera"
    [[ -e /usr/local/share/libcamera/ipa/rpi/vc4/ar0234.json ]] || missing+=" camera-tuning"
    [[ -d /usr/local/lib/python3/dist-packages/libcamera ]] || missing+=" libcamera-python"
    find "/lib/modules/$(uname -r)" -name 'ar0234.ko*' 2>/dev/null | grep -q . || missing+=" camera-driver"
    [[ -e /boot/firmware/overlays/ar0234.dtbo ]] || missing+=" camera-overlay"
    if [[ -n $missing ]]; then
        echo
        echo "!! The camera software is not fully installed (missing:$missing)."
        echo "!! The camera will show an error until you run the full installer (about an hour,"
        echo "!! it skips what is done):   sudo bash ~/benev-install.sh"
        CAMERA_STACK_MISSING=1
    fi
}
CAMERA_STACK_MISSING=0

# --- run -------------------------------------------------------------------------------------

if [[ $UPDATE == 1 ]]; then
    install_runtime_packages
    configure_boot
    install_app
    install_network
    install_ota
    install_service
elif [[ $APP_ONLY == 1 ]]; then
    install_app
    install_network
    install_ota
    install_service
else
    update_system
    install_packages
    enable_interfaces
    install_driver
    build_libcamera
    configure_boot
    install_app
    install_network
    install_ota
    install_service
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "=== Done $(date) ==="
echo "Recorder: http://$(hostname | tr '[:upper:]' '[:lower:]').local  or  http://${IP:-<pi-address>}  (README: $APP_DIR/README.md)"
echo "Without a known Wi-Fi network it starts the open hotspot \"Benev AI\": join it, the setup page opens."
if [[ $UPDATE == 1 || $APP_ONLY == 1 ]]; then
    check_camera_stack
fi
if [[ $UPDATE == 1 ]]; then
    if [[ -e /run/benev-reboot-required ]]; then echo "Boot settings changed: restart the head mount to finish."; fi
elif [[ $APP_ONLY == 0 ]]; then
    echo "A reboot is needed once so the camera driver and PWM overlays load."
    if [[ $NO_REBOOT == 0 ]] && ask "Reboot now?"; then
        echo "Rebooting..."
        reboot
    else
        echo "Reboot later with: sudo reboot"
    fi
fi
exit 0
