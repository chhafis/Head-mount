# BENEV head-mount recorder – installer for a fresh Raspberry Pi Zero 2 W

The BENEV head mount records **video, IMU (960 Hz) and magnetometer data together, on one
clock**, as training data for robotics AI. Start and stop with the **record button** on the head
mount, or from a phone or PC browser (or your own app) at **`http://benev.local`**. If the head
mount can't find a Wi-Fi network it knows, it starts its own hotspot, **"Benev AI"**, where you
pick a network and type its password. LEDs show power, recording and low battery; when USB-C
power is removed for 2 minutes it saves the recording and shuts down by itself. Updates come
**over the air from GitHub**.

`benev-install.sh` turns a freshly flashed Raspberry Pi Zero 2 W into a copy of the original
board: the same drivers, camera stack, web app and Wi-Fi setup. It is one self-contained
file. The app code is packed inside it, and the camera driver and libcamera are downloaded
at fixed, tested versions.

## Downloads

Everything is on the **[Releases page](https://github.com/chhafis/Head-mount/releases/latest)**.
The newest release always has:

| File | For | Direct link to the newest |
|---|---|---|
| `benev-install.sh` | the installer for a fresh Raspberry Pi (see [Install](#install)) | [benev-install.sh](https://github.com/chhafis/Head-mount/releases/latest/download/benev-install.sh) |
| `Benev.apk` | the **Android app** | [Benev.apk](https://github.com/chhafis/Head-mount/releases/latest/download/Benev.apk) |

On the Pi itself, download the installer with:
```
wget https://github.com/chhafis/Head-mount/releases/latest/download/benev-install.sh
```
An installer from a release already knows this repository, so its head mounts update from it.

### Installing the Android app

1. Open the link (or the Releases page) **on the phone** and download `Benev.apk`.
2. Tap the downloaded file. Android asks to allow installing apps from this source (your
   browser or Files app): allow it, go back, and tap *Install*.
3. If Play Protect warns about an unknown app, choose *More details → Install anyway*. The
   app is not from the Play Store, so Android doesn't know it.
4. Connect the phone to the same Wi-Fi as the head mount (or to its "Benev AI" hotspot) and
   open the app.

### Publishing a new version of the app

Replace `android/Benev.apk` with the new build, commit, and make a release (see
[Making a release](#making-a-release)). The release then carries the new app. Two rules, or
phones cannot update over the old app:
- **sign every build with the same key** (the one this APK was signed with), and
- **raise `versionCode`** in the app's build settings each time.

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi Zero 2 W | with the BENEV HAT (Pi Zero 2W Cam Board 1.0) |
| Camera | Arducam B0353 (AR0234), ribbon cable in the **Pi's own** camera socket |
| microSD card | 16 GB or more. Video is up to 25 Mb/s (at most ~190 MB per minute; calm scenes use much less) |
| OS | **Raspberry Pi OS Bookworm**, Lite or Desktop. Tested on 32-bit; 64-bit uses the same steps but has not been tried. Trixie is not supported. |
| Internet on the Pi | Wi-Fi is fine; several hundred MB is downloaded |
| Time | about **1 hour 15 minutes**, most of it compiling libcamera |

## Install

1. **Flash the card** with Raspberry Pi Imager: choose *Raspberry Pi Zero 2 W* and
   *Raspberry Pi OS (Bookworm)*. In the settings, set a username and password, your Wi-Fi,
   and **enable SSH**. The installer names a new Pi `benev`; you can give it another name later
   (see [Naming a head mount](#naming-a-head-mount)).
2. **Copy `benev-install.sh` to the Pi.** Either:
   - over the network, from a Windows PowerShell or terminal:
     `scp benev-install.sh <user>@<pi-address>:~`
   - or before first boot: put it on the card's `bootfs` drive. On the Pi it is then
     `/boot/firmware/benev-install.sh`.

   Copy the file as it is. Do **not** open and save it in a Windows editor: Windows line
   endings break it.
3. **Run it** on the Pi, logged in as your normal user (SSH or a keyboard):
   ```
   sudo bash ~/benev-install.sh
   ```
   (or `sudo bash /boot/firmware/benev-install.sh`).
   - If the system update installs a new kernel, the installer asks you to reboot and run it
     again. It then continues where it stopped.
   - At the end it asks to reboot once more. Say yes.
4. **Open the recorder**: `http://benev.local` (or the IP address the installer prints).
5. **Calibrate once** (see [Calibration](#calibration)) before recording real data.

If a step fails, the installer prints which one. Fix the cause (usually the internet
connection) and run it again. Everything already done is skipped. Full log:
`/var/log/benev-install.log`.

### Options

| Option | Effect |
|---|---|
| `--app-only` | Only update the app, the Wi-Fi manager, the updater and the services; takes seconds. |
| `--update` | What over-the-air updates run: `--app-only` plus any missing packages and boot settings (no system upgrade, no driver rebuild). |
| `--ota-repo OWNER/NAME` | The GitHub repository to take updates from (see [Updates](#updates-over-the-air)). Installers downloaded from a release already know it. |
| `--yes` | No questions; reboots automatically at the end. |
| `--no-reboot` | Never reboot; you reboot yourself later. |
| `--no-upgrade` | Skip `apt full-upgrade` (not recommended on a fresh card). |
| `--user NAME` | Install for that user instead of the one running `sudo`. |
| `--hostname NAME` | Set the name to `NAME` (address `NAME.local`). Without it a new Pi becomes `benev`, and a Pi that already has another name keeps it, so updates never rename a head mount. |
| `--force` | Continue on a board or OS it was not made for. |

## What it installs

1. **System update** (`apt full-upgrade`), so the kernel headers match the running kernel.
2. **Packages from the Raspberry Pi OS repositories**: Python sensor and GPIO libraries
   (smbus2, spidev, libgpiod), numpy, picamera2, PyAV, PiDNG, NetworkManager and avahi,
   and the build tools for the camera driver and libcamera.
3. **I2C and SPI** turned on (`raspi-config`).
4. **Camera driver**: Kurokesu `ar0234-rpi-driver` **v0.1.2** (commit `eefdf9c42cb8`),
   installed with DKMS. It is rebuilt automatically when the kernel updates.
5. **libcamera with AR0234 support**: Kurokesu's fork at commit `409deedcfc90` (version
   0.7.2). It is built for this Pi's ISP and installed in `/usr/local`, *next to* the system
   libcamera, which stays untouched. Only the app's camera process uses it.
6. **`/boot/firmware/config.txt`** (a backup is saved as `config.txt.bak-benev-<date>`):
   - `camera_auto_detect=0`
   - `dtoverlay=ar0234`: camera, 10-bit, 2 lanes
   - `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4`: hardware PWM for the fan and LED2
   - `gpio=23=op,dh` and `dtoverlay=gpio-led,gpio=23,label=benev-power,trigger=default-on`:
     the power-on LED (lit about a second after power-on, off at the very end of shutdown)
7. **The app** in `~/BENEV`, run at boot by the systemd service `benev-dashboard`, on
   ports **80** and 8080. It runs as your user; it gets only the right to use port 80 and
   real-time priority for its two sensor threads (see [Recordings](#recordings-the-folder-format)).
8. **The Wi-Fi manager** `benev-wifi` (runs as root, `/usr/local/lib/benev/wifi_manager.py`),
   the hotspot's captive-portal DNS rule, and **mDNS**: host name `benev`, so
   `http://benev.local` works, plus a DNS-SD advertisement for apps.
9. **The updater** `benev-update` (runs as root, `/usr/local/lib/benev/ota.py`), with a timer
   that checks GitHub every 6 hours.
10. **A sudo rule** in `/etc/sudoers.d/benev-dashboard`. It lets the app user run exactly
    `systemctl poweroff`, `systemctl reboot`, and start the updater's check and install units.
    Nothing else.

## Using the recorder

The app has three pages: **Record** (`/`), **Device** (`/device`) and **Wi-Fi** (`/wifi`).
They are made for phone screens and work without internet (nothing is loaded from outside).

### Record page

From top to bottom:

- **Start recording** with the elapsed time above it. Recording starts the camera, waits
  until it delivers frames, then records video, IMU and magnetometer together. Press again
  to stop. The resolution and frame rate are set on the Device page. The **record button on
  the head mount** does the same (see [LEDs, fan and buttons](#leds-fan-and-buttons)), and
  the page follows along.
- **The cube**: a live 3D view of the head mount (the blue face with the lens is the
  camera). It turns as you turn the head mount: tilt and roll from the IMU, heading from
  the gyroscope and the magnetometer. *Set forward* makes the current direction "straight
  ahead". *North up* shows it relative to magnetic north instead. *Camera faces* says which IMU axis the
  lens looks along (+X by default; this is saved with every recording).
- **Show camera**: optional live preview, with a **Brightness** slider (−2 to +2 stops; *Reset*
  returns to the automatic level) and an **Edge brightening** slider. Changes take effect
  immediately, without a restart, and are saved.
- **Calibrate IMU & magnetometer**: see below.
- **Camera lens**: calibrates this head mount's lens against dark, tinted edges (see
  [Lens shading correction](#lens-shading-correction)).
- **Recordings**: every session with its length, size, frames and IMU rate.
  *Play* shows the video with the cube moving as recorded. *Download* gives a zip of the
  whole folder. *Delete* removes it (tap twice).

### Calibration

Do this once per head mount, and again if you change its build (battery, cables, metal
parts) or move to a very different place. It takes about a minute:

1. Put the head mount down and keep it still. The camera is off for the first seconds,
   then it starts.
2. When asked, pick it up and **slowly turn it in every direction**: tilt forward and back,
   roll left and right, turn around, upside down. The coverage shows how much of the
   sphere you have covered. It finishes by itself at 75 %, or after 45 seconds.
3. The result is saved in `~/BENEV/calibration.json` and used straight away.

It measures:

- **Gyroscope bias**, from the still part.
- **Magnetometer hard and soft iron**: an ellipsoid fit to the readings while turning (a
  sphere fit if the turning was not varied enough).
- **The camera's effect on the magnetometer**: the field with the camera off, minus the
  field with it on.
- **How the magnetometer's axes line up with the IMU's**, found automatically against the
  gyroscope.

#### Magnetometer noise from the camera

The magnetometer sits on the same board as the camera and the Pi. When the camera runs, its
supply current shifts the reading by up to ~20 µT, about half of the Earth's field. The
recorder handles this three ways:

- **Calibrated with the camera on.** The calibration is measured in the state used for
  recording, and the camera-off offset is stored as well. The right one is applied
  depending on whether the camera is running.
- **The magnetometer only steers heading.** Tilt and roll always come from the IMU, so
  magnetic disturbances can never tilt the cube or the recorded orientation.
- **Disturbed readings are ignored.** A reading is used only if its strength is within 15 %
  of the calibrated field and its angle to vertical within 12°. Otherwise (a laptop, steel
  desk or current spike nearby) heading runs on the gyroscope until the field looks right
  again. The badge under the cube says which is happening.

Raw magnetometer values are always saved too, so you can process them differently later.

### Recordings: the folder format

Each recording is a folder `~/BENEV/media/sessions/<YYYYMMDD-HHMMSS>/` (the zip download
holds the same files):

| File | Contents |
|---|---|
| `video.mp4` | H.264 video (`video.mkv` with MJPEG at 1920×1200) |
| `frames.csv` | `frame,t_ns,pts_us,exposure_us,analog_gain,digital_gain,colour_temp_k`: one row per video frame. `t_ns` is the sensor's start-of-exposure time; `pts_us` is the frame's time in the video file. |
| `imu.csv` | `t_ns,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps`: accelerometer (g) and gyroscope (°/s), raw, in IMU axes, **~952 Hz** (the LSM6DSV16B's "960 Hz" rate, its nearest to 1 kHz) with the IMU's own hardware timestamps. The gyro bias is not removed; it is in `calibration.json`. |
| `orientation.csv` | `t_ns,qw,qx,qy,qz,roll_deg,pitch_deg,yaw_deg`: fused orientation for every IMU sample. The quaternion rotates IMU-frame vectors into the earth frame (z up, x magnetic north). |
| `mag.csv` | `t_ns,mx_uT,my_uT,mz_uT,mx_cal_uT,my_cal_uT,mz_cal_uT`: magnetometer at ~25 Hz, raw (sensor axes) and calibrated (IMU axes; equal to raw if not calibrated). |
| `calibration.json` | the calibration in effect for this recording (if any) |
| `session.json` | start time (local and Unix), camera mode, IMU settings, which IMU axis the camera faces, counts, measured IMU rate, largest IMU gap, FIFO overruns and data-quality counters (`imu_fifo_errors`; `ts_jump` = samples the IMU itself skipped) |

**All `t_ns` are on the same clock** (Linux `CLOCK_MONOTONIC`, nanoseconds), so the files
line up by timestamp directly. To find the IMU data for a frame, look up its `t_ns` in
`imu.csv`. `session.json` has `start_unix_ns` and `start_monotonic_ns` to convert to wall
time. Sensor logging starts just before the first frame and stops after the last, so every
frame has IMU data on both sides.

Typical results: IMU 952.9 Hz, no FIFO overruns, and video frames matching
`frames.csv` within 0.04 ms. In a 2-minute recording with the live preview and
playback downloads running at the same time (worst case), 2 of 115,000 IMU samples were missing
(largest gap 2.1 ms), shown as `ts_jump` in `session.json`. Without that extra load, none.

Why the app needs real-time priority: if another task interrupts an SPI transfer to the IMU
half-way, the IMU drops the sample due at that moment. Under full CPU load that cost ~10 samples
a minute. So the IMU and magnetometer threads, and the kernel's SPI thread, run at real-time
priority 20 (they sleep between reads, so they take very little CPU).

### Device page

| Section | What it does |
|---|---|
| Fan & LEDs | The automatic outputs with what they are doing and why (fan, power-on, record and battery-low LEDs); the fan's normal speed; LED2 brightness and the spare green LED by hand. |
| Power & buttons | USB-C power or battery, the shutdown countdown, how long to run on battery before shutting down, the battery-low threshold, and how often each button was pressed (to test them). |
| Software update | Installed version, the newest release on GitHub, *Check for updates*, *Install*, and *Restart now* when an update changed boot settings. |
| Camera | Live view, **Take photo**, a plain video **Record / Stop** (video only), resolution and frame rate for both, optional raw DNG with photos, the **Camera** selector: *Camera 1* is the standard orientation and *Camera 2* turns the picture by 180° (the lens and edge calibrations follow the orientation; the setting is saved with every recording), and the list of photos and plain videos. The *Lens shading correction* box calibrates the lens. |
| Battery | MAX17048: charge %, voltage, charge rate, alert, 1-hour history. |
| Motion | LSM6DSV16B: accelerometer, gyroscope, roll/pitch, temperature. |
| Magnetic field | MMC5983MA, offset-corrected X/Y/Z and magnitude. |
| Restart Pi / Shut down Pi | Stop and save a running recording first (tap twice). |

### Photo and video quality

- **Photos**: full 1920×1200 sensor readout, high-quality denoise, JPEG quality 95, plus a
  raw DNG if ticked.
- **Video**: hardware H.264 High profile (up to 25 Mb/s) in `.mp4`. At 1920×1200 it is MJPEG
  in `.mkv`, because the Pi's H.264 encoder cannot take that size.
- Frame rates are limited to what records with **no dropped frames** (measured on the Zero 2 W):

| Resolution | Max fps | Field of view |
|---|---|---|
| 1920×1200 | 25 | full sensor |
| 1920×1080 | 30 | crop: Camera 1 keeps the lower 1080 lines of the 1920×1200 readout (120 lines are cut from the top, where its lens gives the least light); Camera 2 uses the sensor's centred 1080-line mode |
| 1280×720 | 60 | crop |
| 960×600 | 100 | full (2×2 binned) |

## Lens shading correction

A wide-angle lens delivers less light to the edges of the sensor and shifts their colour (pink,
green or purple). The Raspberry Pi image processor (ISP) can compensate, but it needs a
measurement of the specific lens. The stock AR0234 tuning contains none, so each head mount is
calibrated once on the Device page.

**Procedure**

1. Cover the whole lens with a white diffuser, for example two to three layers of white printer
   paper, or the base of a white plastic cup.
2. Point the head mount at an even, steady light source, such as an overcast window or a ceiling
   lamp, and keep it still. Avoid mixing daylight and artificial light. The light source must not
   appear as a bright spot through the diffuser: add layers of paper, or hold the diffuser against a
   screen showing pure white. A bright spot causes the calibration to be rejected or leaves a dark
   patch in the centre of the picture.
3. Press **Calibrate** under *Lens shading correction* on the Device page, or **Calibrate this
   camera's lens** in the *Camera lens* card on the Record page. The measurement takes about
   15 seconds.

Each camera and lens combination has its own vignetting pattern, so **every head mount is
calibrated individually**. The result is stored on the head mount only
(`~/BENEV/lens-shading.json`), is preserved by updates, and is excluded from version control.

After calibration the page reports how uneven the picture was, and the preview, photos and
recordings are corrected. A calibration is rejected, and the previous one kept, if the scene is too
dark, over-exposed or not an even white; the message states what to correct. *Remove* reverts to
the uncorrected picture.

**Method.** The calibration averages 8 full-size raw frames, measures brightness and the
red/blue balance on a 16×12 grid, and writes gain tables into a copy of the sensor's tuning file
(`~/BENEV/camera-tuning.json`). This follows the method of Raspberry Pi's tuning tool
(`rpi.alsc`), for a single colour temperature; under very different lighting the ISP adjusts the
colour tables itself.

**Limits**

- The ISP can raise the edges by a factor of 4 at most. If the lens's image circle does not reach
  the corners of the sensor, those areas remain darker, and the page reports their share of the
  picture. With a lens whose image circle covers the sensor (1/2.6" format or larger for the
  AR0234) this share is close to zero. For lenses that fall short, see *Edge brightening* below.
- The correction addresses shading, not lens distortion (the fisheye projection).
- **Lens centring (mechanical vignetting).** If the lens holder sits off-centre over the sensor,
  one side of the sensor is lit well and the opposite side receives little or no light, and no
  software can recover it. The calibration measures where the lit area is centred and, when it is
  100 px or more from the sensor centre, reports the offset in pixels and millimetres (the
  AR0234's pixels are 3 µm). Loosen the lens holder, re-centre it, and calibrate again. A lens
  whose chief ray angle suits the sensor, and a clear light path (no hood, filter or barrel
  clipping the view), reduce the remaining vignetting.

### Edge brightening

For lenses whose image circle is smaller than the sensor, the **Edge brightening** slider (Record
and Device pages) applies a further digital gain of up to 8× to the dark areas. During lens
calibration the head mount measures the remaining shading with the ISP correction active and stores
it as a 20×32 gain map (`edge_gain` in `lens-shading.json`), so the map is specific to each lens.
Where the gain is high the noise grows with it, so the filter also averages each pixel with its
neighbours and reduces colour there, in proportion to the gain (as camera image processors do with
lens shading gain). The lifted edges are therefore smoother and less detailed than the centre. The
feature is switched on by a lens calibration that leaves much of the picture dark, and can be
lowered or turned off with the slider.

- **Scope.** Edge brightening applies to the live preview and to photos. Recorded video is not
  modified: processing a 1080p frame takes 35–40 ms on the Raspberry Pi Zero 2 W, which exceeds
  the 33 ms available per frame at 30 fps, and 1920×1080 at 30 fps takes priority.
- **Brightening recorded video.** Every recording stores the map as `edge_gain.json` in its
  folder. On a PC with `pip install av numpy pillow`, run
  `python tools/edge_boost_video.py <recording folder>` to write `video_edge.mp4` beside the
  original, with identical frames and timing (`--strength 0.5` applies half the gain). The files
  `edge_boost.py`, `edge_boost.c` and the `tools` folder from `~/BENEV` are required.
- **Areas that receive no light.** Where the lens delivers essentially no light, the gain fades out
  rather than amplifying noise, so those areas remain black. Only a lens with a larger image
  circle, or cropping the picture, resolves this.
- **Requirement.** The installer compiles a small routine (`libedgeboost.so`) with `gcc`. If it is
  unavailable the slider reports that edge brightening is not available.

## LEDs, fan and buttons

| Part | Pin | Behaviour |
|---|---|---|
| Power-on LED (PWR_ON_G) | GPIO23 | On about a second after power-on. Off at the very end of a shutdown: **when it goes off, the Pi is safe to unplug.** |
| Record LED (REC_LED) | GPIO24 | On while recording. Blinks while a recording starts or stops (the camera takes 2–5 s to start). Flickers for 2 s if a recording could not start. |
| Battery-low LED (BAT_LOW_R) | GPIO16 | On below 15 % battery (off again above 18 %). **Blinks slowly while running on battery** and counting down to the automatic shutdown. |
| Fan (FAN_CTRL) | GPIO12 | 50 % from start-up. **Full speed from 60 °C** CPU temperature, back to normal at 52 °C. Set the normal speed with the fan slider on the Device page. |
| Record button (SW1, CAM_REC) | GPIO4 | Press: start a recording. Press again: stop and save it. Presses within 1 s of the last one are ignored. |
| Power button (SW2) | GPIO22 (LTC2954 INT) | **Press to switch on.** Short press while on: save any recording, shut down, and switch the board off. Holding it for about 14 s forces the board off without a safe shutdown (use only if the Pi has hung). Requires the LTC2954 (U13). |
| Power off (PI_KILL) | GPIO5 | Driven low by the Pi at the very end of a power-off, so the LTC2954 switches the whole board off. Never driven during a reboot. |

The LED colours are whatever is fitted on D8/D9; the names come from the schematic. The
thresholds are in `~/BENEV/board.json`; the battery and shutdown ones are also on the Device
page.

## Power and shutting down

The charger's PGOOD signal (GPIO6) tells the Pi whether USB-C power is connected. When it is
removed:

1. The battery-low LED starts blinking, and the Record page shows **"On battery · off in 1:59"**.
2. If power comes back within **2 minutes**, nothing happens.
3. Otherwise the recording is stopped and saved (video and all sensor files), and the Pi shuts
   down. When the power-on LED goes off, it is done.

Change the 2 minutes on the Device page (1, 2, 5, 10 or 30 minutes, or never). It also applies
when the Pi starts on battery. While on battery, updates are not installed.

Every power-off ends with the board switching itself off completely: as the last step, after
all files are saved and the file systems are read-only, the Pi pulls PI_KILL (GPIO5) low and the
LTC2954 (U13) cuts all rails. The board then draws no power from the battery for the Pi or the
camera. **To switch it on again, press the power button.** Plugging in USB-C alone does not start
it. *Restart Pi* keeps the power on.

The step is a systemd shutdown hook (`/usr/lib/systemd/system-shutdown/benev-poweroff`). On a
board without U13, the PI_KILL line goes nowhere, so the board stays powered after a shutdown until
power is removed.

Every way of stopping keeps the recording: *Shut down Pi* / *Restart Pi* on the page, the
power button, running out the battery countdown, an update, and even `sudo reboot` over SSH.

## Wi-Fi

At power-on:

1. The head mount joins a **saved Wi-Fi network** if one is in range (within about a minute).
2. If none is, it starts the hotspot **"Benev AI"** (open, no password).
3. Join "Benev AI" with your phone. The phone shows a *Sign in to network* page, or you
   open any `http://` address. You land on the **Wi-Fi page**.
4. Tap your network, type its password, *Connect*. The head mount leaves the hotspot and
   joins that network, so your phone drops off "Benev AI". Connect the phone to the same
   network and open `http://benev.local`.
   - If joining fails (wrong password, out of range), the hotspot comes back within about a
     minute. Join it again and the Wi-Fi page shows the reason.
5. Every network you connect to is remembered and joined by itself next time.

Later:

- **Signal lost.** If the Wi-Fi network is gone for a minute, the hotspot starts.
- **Back in range.** While the hotspot is on and **no device is connected to it**, the head
  mount looks for saved networks every 90 s and switches back when one is in range. It never
  switches while your phone is on the hotspot.
- **Recording without a router** (outdoors): just use the hotspot. Join "Benev AI" and open
  `http://10.42.0.1` (or `http://benev.local`). Recording does not need any network.
- **Turn on the hotspot** on the Wi-Fi page starts it on purpose. It then stays on until you
  pick a network or tap *Use saved Wi-Fi*.
- **Forget** removes a saved network.

The hotspot is **open** by default, so anyone nearby can join it and use the recorder. To
give it a password, edit `/etc/benev/wifi.json` on the Pi:
```
{
  "hotspot_ssid": "Benev AI",
  "hotspot_password": "at-least-8-characters"
}
```
then `sudo systemctl restart benev-wifi`. While the hotspot runs, every DNS name points at
the head mount; that is what makes phones open the setup page. Nothing is forwarded to the
internet.

## Naming a head mount

Each head mount has a **name**. It is also its network address (`http://NAME.local`), the heading
and tab title of every page, and its name in apps that discover head mounts (the DNS-SD service
name). When several head mounts are in use, give each a distinct name, for example `benev101`,
`benevtest` or `head`.

To change it, open the **Device** page, enter the name in the *Device name* card and press
*Save name*. The change takes about 3 seconds; then open the new address shown on the page. The old
address stops working. Names may contain letters, digits and hyphens, up to 63 characters, and may
not start or end with a hyphen; capital letters are converted to lower case. If another device on
the network already uses the name, the page says so and the head mount announces itself as
`NAME-2` until a different name is chosen.

The name is the Pi's host name, so it persists across restarts and updates. The Wi-Fi hotspot is
named "Benev AI" on every head mount; to distinguish them there, change `hotspot_ssid` in
`/etc/benev/wifi.json` (see [Wi-Fi](#wi-fi)).

## Updates over the air

Head mounts update themselves from the **GitHub Releases** of
[chhafis/Head-mount](https://github.com/chhafis/Head-mount). A release is made by GitHub
Actions (`.github/workflows/release.yml`) whenever you push a version tag.

### Which head mounts update from here

- Installed with a `benev-install.sh` **from a release**, or built on a PC whose git `origin`
  is this repository: yes, by themselves.
- Installed with an older build that doesn't know the repository (the Device page says
  *No update source*): tell it once, over SSH:
  ```
  sudo bash ~/benev-install.sh --app-only --ota-repo chhafis/Head-mount
  ```
  or edit `"repo"` in `/etc/benev/ota.json`. Only root can change it, on purpose: the web page
  must not be able to choose what code runs on the head mount.

The folder is in git with `origin` = `https://github.com/chhafis/Head-mount.git`.
`.gitattributes` keeps Linux line endings even on Windows; the built `benev-install.sh` is not
committed (releases carry it).

### Making a release

```
git commit -am "What changed"
git tag v1.0.0
git push origin main v1.0.0
```
About a minute later the release appears under *Releases* with `benev-install.sh` and its
SHA-256. That same file is also the installer for a fresh Pi. Versions must look like
`v1.2.3` and go up; head mounts install only newer versions.

### On the head mount

- Every 6 hours it checks for a new release; *Check for updates* on the Device page checks now.
- **Install** shows when one is available. Installing takes a minute or two; the app restarts.
  Afterwards *Restart now* appears if the update changed boot settings (`config.txt`).
- It **refuses to install while recording or running on battery**.
- **Every download is verified** against its SHA-256 (and its signature, if you set up
  signing). Then the current app files are backed up. If the installer fails, or the app does
  not come back within 90 s, the backup is **restored automatically**. The Device page then
  shows *Update failed, rolled back* and the installer's last lines.
- Automatic installs: set `"auto_install": true` in `/etc/benev/ota.json`. It then installs
  when it finds an update, if nothing is recording and USB-C power is connected (otherwise at
  the next check).
- From SSH: `sudo benev-update check`, `sudo benev-update install`. State and logs:
  `/var/lib/benev/ota/`, `journalctl -u 'benev-update@*'`.

An update replaces the app, the Wi-Fi manager, the updater, the services and missing boot
settings, and installs missing packages. The camera driver, libcamera and the OS are not touched
(for those, run the full installer).

### Private repository

Create a fine-grained token on GitHub (Settings → Developer settings → Fine-grained tokens)
with **read-only** access to the repository's *Contents*. Put it in `/etc/benev/ota.json` on
each head mount as `"token": "github_pat_..."`. The file is readable by root only.

### Signed releases (optional, recommended for many head mounts)

Without signing, a head mount trusts whatever is published as a release in the repository.
With signing, a release also needs a signature from a key you keep, and head mounts reject
anything else.
1. Make a key pair (keep the private key out of the repository):
   `ssh-keygen -t ed25519 -N "" -C benev-release -f ~/benev-ota-key`
2. In the repository settings → *Secrets and variables* → *Actions*, add the secret
   `OTA_SIGNING_KEY` containing the whole private key file `~/benev-ota-key`.
3. Create `source/app/BENEV/system/ota_allowed_signers` containing one line:
   `benev-release namespaces="benev-ota" ` followed by the contents of `~/benev-ota-key.pub`.
   Commit it and make a release. From then on, head mounts that installed it accept only
   signed releases.

## Building an Android app

The app is a normal web app, so an Android app can show it in a WebView or use the JSON API
directly.

**Finding the head mount**

- **Same Wi-Fi network**: `http://benev.local`. Or discover it with
  [`NsdManager`](https://developer.android.com/develop/connectivity/wifi/use-nsd) (DNS-SD).
  It advertises **`_benev._tcp`** (port 80, TXT `path=/`, `api=/api`) and `_http._tcp`, with
  the host name as the service name. Discovery works on every Android version and with
  several head mounts. `.local` names do not resolve on some older Android versions.
- **On the "Benev AI" hotspot**: always **`http://10.42.0.1`**.
- The phone's own address on the hotspot is 10.42.0.10–254.

**Important on Android**: the hotspot has no internet, so Android keeps sending app traffic
over mobile data. Request the Wi-Fi network and bind to it before you connect:
`ConnectivityManager.requestNetwork()` with `TRANSPORT_WIFI`, then
`bindProcessToNetwork(network)`. Or use `network.openConnection()` / OkHttp's
`socketFactory(network.socketFactory)`. Also allow cleartext HTTP to the head mount
(`android:usesCleartextTraffic`, or a network security config for `benev.local` and
`10.42.0.1`).

The pages use no browser dialogs (`alert`/`confirm`): delete and shut down ask for a second
tap instead. So they work unchanged inside a WebView.

**API** (all JSON; POST bodies need `Content-Type: application/json`):

| Request | What it does |
|---|---|
| `GET /api/stream` | Server-Sent Events, about 25 messages/s: `orientation` (quaternion `q`, roll/pitch/yaw, rates), `imu`, `mag`, `battery`, `session` (recording state and seconds). Once a second also `camera`, `calibration`, `wifi`, `disk`. |
| `POST /api/session` `{"action":"start"}` / `{"action":"stop"}` | start / stop a recording |
| `GET /api/sessions` | list of recordings |
| `GET /api/sessions/<id>/download` | zip of one recording |
| `GET /api/sessions/<id>/files/<name>` | one file (`video.mp4`, `imu.csv`, …) |
| `GET /api/sessions/<id>/track?hz=30` | orientation aligned to the video (seconds from the first frame), for playback |
| `POST /api/sessions/delete` `{"id":"<id>"}` | delete a recording |
| `POST /api/calibration` `{"action":"start"\|"cancel"\|"tare"\|"reset_tare"}` | calibration wizard; progress comes in the stream as `calibration_run` |
| `GET /api/camera.mjpg` | live preview (MJPEG) |
| `GET /api/wifi`, `POST /api/wifi` `{"action":"scan"\|"connect"\|"forget"\|"hotspot"\|"rejoin", "ssid", "password"}` | Wi-Fi status and setup |
| `GET /api/status` | one small object: version, recording, power/battery state, calibrated, Wi-Fi mode |
| `GET /api/board`, `POST /api/board` `{"unplug_shutdown_s", "battery_low_pct", "fan_normal_pct", "fan_full_c", "fan_back_c"}` | power, buttons, fan and LED settings |
| `GET /api/update`, `POST /api/update` `{"action":"check"\|"install"}` | software update state and actions |
| `POST /api/system` `{"action":"shutdown"\|"reboot"}` | save any recording, then power off / restart |

## Files and places

| Path | Contents |
|---|---|
| `~/BENEV/` | app code (`server.py`, `sensors.py`, `motion.py`, `recorder.py`, `board.py`, `outputs.py`, `camera.py`, `camera_worker.py`, `static/`, `system/`, `tools/`), `VERSION` |
| `~/BENEV/board.json` | fan, battery-LED and shutdown settings (kept by updates) |
| `~/BENEV/lens-shading.json`, `camera-tuning.json` | lens shading calibration and the tuning file built from it (kept by updates) |
| `/etc/benev/ota.json` | update source, token, automatic install (root only) |
| `/var/lib/benev/ota/` | update state, last installer log, backup of the previous version |
| `/etc/systemd/system/benev-update@.service`, `benev-update.timer`, `/usr/local/lib/benev/ota.py` | the updater |
| `~/BENEV/media/sessions/` | recordings (one folder each) |
| `~/BENEV/media/` | photos and plain videos from the Device page |
| `~/BENEV/calibration.json` | the calibration (kept by `--app-only`) |
| `~/BENEV/camera.json`, `outputs.json` | saved camera mode and fan/LED states (kept by `--app-only`) |
| `/etc/systemd/system/benev-dashboard.service` | the app service |
| `/etc/systemd/system/benev-wifi.service`, `/usr/local/lib/benev/wifi_manager.py` | the Wi-Fi manager |
| `/etc/benev/wifi.json` | hotspot name and password |
| `/etc/NetworkManager/system-connections/` | saved Wi-Fi networks (`benev-hotspot.nmconnection` is the hotspot) |
| `/etc/NetworkManager/dnsmasq-shared.d/benev-captive.conf` | captive-portal DNS, only active while the hotspot is on |
| `/etc/avahi/services/benev.service` | the DNS-SD advertisement |
| `/usr/local/src/ar0234-rpi-driver`, `/usr/local/src/libcamera-kurokesu` | driver and libcamera sources (keep them; uninstall needs them) |
| `/var/log/benev-install.log` | installer log |

Service commands:
```
sudo systemctl restart benev-dashboard     # restart the app
journalctl -u benev-dashboard -f           # its live log
journalctl -u benev-wifi -f                # Wi-Fi decisions (hotspot on/off, joins, failures)
sudo systemctl disable --now benev-dashboard   # stop and don't start at boot
```

## Changing the app

The code bundled in the installer is in `source/app/BENEV/`. After editing it, rebuild the
installer and update a Pi:
```
bash source/build.sh                       # writes a new benev-install.sh (Git Bash or Linux)
scp benev-install.sh <user>@benev.local:~
ssh <user>@benev.local sudo bash benev-install.sh --app-only
```

## Troubleshooting

- **`benev.local` does not open**: try the IP address (shown on the Wi-Fi page, or in your
  router). Windows 10/11, macOS, iOS and Linux resolve `.local`. On Android use the app's
  discovery or the IP. Check `systemctl status avahi-daemon`.
- **The hotspot does not appear**: it starts only when no saved network is in range, about a
  minute after power-on. If a saved network is in range but its password changed, the hotspot starts
  after the failed attempts; see `journalctl -u benev-wifi`.
- **The phone joins "Benev AI" but no page opens**: open `http://10.42.0.1` yourself. If
  that does not load either, turn off mobile data for a moment (Android may send the
  browser's traffic there).
- **Camera says "Driver not loaded"**: run `dmesg | grep ar0234`. You should see
  `Success reading chip id: 0xa56`. If not, check the ribbon cable (power off first), and
  that `/boot/firmware/config.txt` contains `dtoverlay=ar0234`. Reboot after any change.
- **Fan and LED2 have no slider**: the PWM overlay loads only at boot. Reboot once.
- **A sensor shows "Not responding"**: the card shows the error. Check that I2C/SPI are
  enabled (`ls /dev/i2c-1 /dev/spidev0.*`) and the HAT is seated.
- **The record button does nothing**: the Device page counts presses under *Power & buttons*.
  If the count does not go up, check SW1 and R10 (GPIO4 should read 1 released, 0 pressed:
  `pinctrl get 4`). If it counts but nothing records, the reason is in `journalctl -u benev-dashboard`.
- **The power button does nothing**: it needs the LTC2954 (U13) fitted.
- **It shut down by itself**: it ran on battery (no USB-C power) for longer than the set time;
  `journalctl -b -1 -u benev-dashboard | grep "shutting down"` shows the reason.
- **An update failed**: the Device page shows the installer's last lines, and the previous
  version was restored. Full log: `/var/lib/benev/ota/install.log`.
- **The cube's heading drifts**: calibrate. Until then heading comes from the gyroscope
  only. If the badge under the cube often says the field is disturbed, calibrate again
  where you record, away from steel and laptops.
- **The installer stops at the kernel headers**: run `sudo apt update && sudo apt full-upgrade -y && sudo reboot`,
  then the installer again.

### Known hardware behaviour

- The magnetometer reads up to ~20–30 µT off while the CPU or camera is busy, because it
  picks up the board's supply current. The calibration and filter handle the camera part
  (see [Magnetometer noise from the camera](#magnetometer-noise-from-the-camera)).
- The accelerometer reads about 1.07 g at rest (a ~65 mg offset of the part).
- If the fuel gauge (MAX17048) returns corrupted I2C data, this indicates a hardware fault on
  that board rather than a software problem. The app rejects the invalid readings.
- On a board without the LTC2954 (U13), the HAT stays powered after *Shut down Pi*, and the
  power button does nothing.

## Uninstall

```
sudo systemctl disable --now benev-dashboard benev-wifi benev-update.timer
sudo nmcli connection delete benev-hotspot
sudo rm /etc/systemd/system/benev-dashboard.service /etc/systemd/system/benev-wifi.service \
        /etc/systemd/system/benev-update@.service /etc/systemd/system/benev-update.timer \
        /etc/sudoers.d/benev-dashboard /etc/NetworkManager/dnsmasq-shared.d/benev-captive.conf \
        /etc/avahi/services/benev.service /usr/local/bin/benev-update \
        /usr/lib/systemd/system-shutdown/benev-poweroff
sudo rm -r /usr/local/lib/benev /etc/benev /var/lib/benev
sudo ninja -C /usr/local/src/libcamera-kurokesu/build uninstall && sudo ldconfig
sudo dkms remove ar0234-rpi-dkms/0.1.2 --all && sudo rm /boot/firmware/overlays/ar0234.dtbo
sudo cp /boot/firmware/config.txt.bak-benev-<date> /boot/firmware/config.txt   # the backup it made
rm -rf ~/BENEV     # deletes your recordings and photos too
```
Saved Wi-Fi networks stay. The host name stays `benev`; change it with `sudo raspi-config`.
