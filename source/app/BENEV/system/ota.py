#!/usr/bin/env python3
"""Benev over-the-air updates from GitHub Releases.

Runs as root, from benev-update@<action>.service (installed to /usr/local/lib/benev/ota.py;
also `sudo benev-update <action>`):
  check    ask GitHub for the latest release and note whether it is newer than this one
  install  check, then download, verify and install it (refused while recording or on battery)
  auto     what benev-update.timer runs: check, and install too if "auto_install" is on

A release is a tag vX.Y.Z pushed to the repository: .github/workflows/release.yml builds
benev-install.sh and attaches it with its SHA-256 (and a signature, if a signing key is set
up). Installing runs `benev-install.sh --update`: app, Wi-Fi manager, this updater, boot
settings, services, missing packages; no system upgrade and no driver rebuild. The app files
are backed up first and restored if the dashboard does not come back afterwards.

Config, /etc/benev/ota.json (root only; written by the installer):
  {"repo": "owner/name", "token": "", "auto_install": false, "user": "benev"}
  token: only for a private repository (fine-grained token, read-only access to contents).
Signatures: if /etc/benev/ota_allowed_signers exists, releases must carry benev-install.sh.sig
  made with `ssh-keygen -Y sign -n benev-ota` by a key listed there (identity benev-release).
State for the dashboard: /var/lib/benev/ota/state.json (world-readable, no secrets).
"""
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path("/etc/benev/ota.json")
SIGNERS = Path("/etc/benev/ota_allowed_signers")
STATE_DIR = Path("/var/lib/benev/ota")
STATE = STATE_DIR / "state.json"
REBOOT_FLAG = Path("/run/benev-reboot-required")   # set by the installer, gone after a reboot
API = os.environ.get("BENEV_OTA_API", "https://api.github.com")   # override: tests only
ASSET = "benev-install.sh"
DASHBOARD = "http://127.0.0.1:8080"
HEADERS = {"User-Agent": "benev-ota", "X-GitHub-Api-Version": "2022-11-28"}
HEALTH_TIMEOUT_S = 90
# Restored on rollback (besides the app code): what `benev-install.sh --update` rewrites.
SYSTEM_FILES = ["/usr/local/lib/benev", "/etc/systemd/system/benev-dashboard.service",
                "/etc/systemd/system/benev-wifi.service", "/etc/systemd/system/benev-update@.service",
                "/etc/systemd/system/benev-update.timer", "/etc/sudoers.d/benev-dashboard"]
APP_STATE = {"media", "calibration.json", "camera.json", "outputs.json", "board.json",
             "lens-shading.json", "camera-tuning.json"}


class UpdateError(Exception):
    pass


# --- state and config --------------------------------------------------------------------------

def read_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    state = {**read_state(), **changes, "updated": round(time.time())}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE)
    print(f"[{state.get('status')}] {state.get('message', '')}", flush=True)
    return state


def load_config():
    config = {"repo": "", "token": "", "auto_install": False, "user": ""}
    try:
        config.update(json.loads(CONFIG.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        raise UpdateError(f"{CONFIG} is not valid JSON: {e}")
    if config["repo"] and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repo"]):
        raise UpdateError(f"repo in {CONFIG} must look like owner/name")
    return config


def app_dir(config):
    user = config["user"] or os.environ.get("SUDO_USER") or "benev"
    return user, Path(pwd.getpwnam(user).pw_dir) / "BENEV"


def current_version(directory):
    try:
        return (directory / "VERSION").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return "unknown"


def parse_version(v):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    if a is None:
        return False
    return b is None or a > b   # a development build is older than any release


# --- GitHub ------------------------------------------------------------------------------------

def github_json(path, token):
    headers = {**HEADERS, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no release found: check the repository name (a private repository also needs a token)")
        if e.code in (401, 403):
            raise UpdateError(f"GitHub refused the request ({e.code}): bad token or rate limit; try again later")
        raise UpdateError(f"GitHub answered {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"cannot reach GitHub ({getattr(e, 'reason', e)}); is the head mount online?")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(asset, token, dest):
    """Fetch a release asset. With a token (private repository) the API answers with a redirect
    to a signed URL, which must be fetched without the token."""
    try:
        if token:
            req = urllib.request.Request(asset["url"], headers={
                **HEADERS, "Accept": "application/octet-stream", "Authorization": f"Bearer {token}"})
            try:
                resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code not in (301, 302, 303, 307, 308):
                    raise
                resp = urllib.request.urlopen(urllib.request.Request(e.headers["Location"], headers=HEADERS), timeout=60)
        else:
            resp = urllib.request.urlopen(urllib.request.Request(asset["browser_download_url"], headers=HEADERS), timeout=60)
        with resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 16)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"download of {asset['name']} failed: {getattr(e, 'reason', e)}")
    if dest.stat().st_size != asset["size"]:
        raise UpdateError(f"{asset['name']} is incomplete ({dest.stat().st_size} of {asset['size']} bytes)")


def check(config, directory):
    if not config["repo"]:
        save_state(status="not-configured", message=f"No update source: set \"repo\" in {CONFIG}",
                   current=current_version(directory), repo="", auto_install=bool(config["auto_install"]),
                   latest=None, log_tail=None, error=None)
        return None
    current = current_version(directory)
    save_state(status="checking", message=f"Checking {config['repo']} for updates", current=current,
               repo=config["repo"], auto_install=bool(config["auto_install"]), error=None, log_tail=None)
    rel = github_json(f"/repos/{config['repo']}/releases/latest", config["token"])
    latest = {"tag": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"),
              "notes": (rel.get("body") or "")[:4000], "published": rel.get("published_at"),
              "url": rel.get("html_url")}
    assets = {a["name"]: a for a in rel.get("assets", [])}
    newer = is_newer(latest["tag"], current)
    if newer and ASSET not in assets:
        raise UpdateError(f"release {latest['tag']} has no {ASSET}")
    save_state(status="available" if newer else "up-to-date", latest=latest, checked_at=round(time.time()),
               message=f"{latest['tag']} is available (installed: {current})" if newer
               else f"Up to date ({current}; latest release {latest['tag']})")
    return (latest, assets) if newer else None


# --- install -----------------------------------------------------------------------------------

def dashboard_status():
    try:
        with urllib.request.urlopen(DASHBOARD + "/api/status", timeout=5) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def not_safe_reason():
    """Why installing now would be a bad idea, or None."""
    s = dashboard_status()
    if s is None:
        return None   # the dashboard is down; an update may well fix it
    if s.get("recording") or s.get("session_busy"):
        return "a recording is running; stop it first"
    board = s.get("board") or {}
    if board.get("shutting_down"):
        return "the head mount is shutting down"
    if board.get("external_power") is False:
        return "the head mount runs on battery; connect USB-C power first"
    return None


def verify(path, assets, token, work):
    if "benev-install.sh.sha256" not in assets:
        raise UpdateError("the release has no SHA-256 checksum file")
    sums = work / "benev-install.sh.sha256"
    download(assets["benev-install.sh.sha256"], token, sums)
    expected = sums.read_text().split()[0].lower()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise UpdateError("checksum mismatch: the download is corrupt or was altered")
    if SIGNERS.exists():
        if "benev-install.sh.sig" not in assets:
            raise UpdateError("this head mount requires signed releases, and this one is not signed")
        sig = work / "benev-install.sh.sig"
        download(assets["benev-install.sh.sig"], token, sig)
        with open(path, "rb") as f:
            r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(SIGNERS), "-I", "benev-release",
                                "-n", "benev-ota", "-s", str(sig)], stdin=f, capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError(f"bad signature: {(r.stderr or r.stdout).strip()}")
    head = path.read_bytes()[:200]
    if not head.startswith(b"#!/bin/bash") or b"__BENEV_PAYLOAD_BELOW__" not in path.read_bytes():
        raise UpdateError(f"{ASSET} does not look like a Benev installer")
    if subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode != 0:
        raise UpdateError(f"{ASSET} has a syntax error")


def backup(directory, dest):
    def skip_state(info):
        rel = Path(info.name).relative_to(str(directory).lstrip("/"))
        return None if rel.parts and rel.parts[0] in APP_STATE else info
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(directory, arcname=str(directory).lstrip("/"), filter=skip_state)
        for p in SYSTEM_FILES:
            if os.path.exists(p):
                tar.add(p, arcname=p.lstrip("/"))


def restore(src, directory):
    # Files the update added are left in place; everything it changed goes back.
    with tarfile.open(src) as tar:   # our own archive
        try:
            tar.extractall("/", filter="tar")
        except TypeError:   # Python without extraction filters (3.11.2 on Bookworm)
            tar.extractall("/")
    subprocess.run(["systemctl", "daemon-reload"])
    subprocess.run(["systemctl", "restart", "benev-wifi.service", "benev-dashboard.service"])


def healthy():
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        active = all(subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0
                     for u in ("benev-dashboard.service", "benev-wifi.service"))
        if active and dashboard_status() is not None:
            return True
        time.sleep(3)
    return False


def install(config, directory, user, found):
    latest, assets = found
    work = STATE_DIR / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    installer = work / ASSET
    save_state(status="downloading", message=f"Downloading {latest['tag']}")
    download(assets[ASSET], config["token"], installer)
    save_state(status="verifying", message=f"Verifying {latest['tag']}")
    verify(installer, assets, config["token"], work)
    before = current_version(directory)
    save_state(status="installing", message=f"Installing {latest['tag']} (the app restarts)")
    saved = STATE_DIR / "backup.tar.gz"
    backup(directory, saved)
    try:
        with open(STATE_DIR / "install.log", "w") as log:
            rc = subprocess.run(["bash", str(installer), "--update", "--yes", "--user", user],
                                stdout=log, stderr=subprocess.STDOUT, timeout=3600).returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    if rc != 0 or not healthy():
        why = f"the installer failed (exit {rc})" if rc != 0 else "the app did not come back"
        save_state(status="installing", message=f"{why}; restoring {before}")
        restore(saved, directory)
        back = "was restored and is running" if healthy() else "was restored but the app is not answering"
        tail = (STATE_DIR / "install.log").read_text(errors="replace").splitlines()[-15:]
        save_state(status="rolled-back", message=f"Update to {latest['tag']} failed: {why}. {before} {back}.",
                   error=why, log_tail=tail, current=current_version(directory))
        return
    shutil.rmtree(work, ignore_errors=True)
    save_state(status="installed", current=current_version(directory), error=None, log_tail=None,
               reboot_required=REBOOT_FLAG.exists(),
               message=f"Installed {latest['tag']}" + (". Restart the head mount to finish (boot settings changed)."
                                                       if REBOOT_FLAG.exists() else ""))


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action not in ("check", "install", "auto"):
        sys.exit(f"usage: {sys.argv[0]} check|install|auto")
    if os.geteuid() != 0:
        sys.exit("run as root")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another update run is in progress")
        return
    try:
        config = load_config()
        user, directory = app_dir(config)
        found = check(config, directory)
        if found and (action == "install" or (action == "auto" and config["auto_install"])):
            reason = not_safe_reason()
            if reason and action == "auto":
                save_state(status="available", message=f"{found[0]['tag']} is available; it installs "
                                                         f"automatically when possible ({reason})")
                return
            if reason:
                raise UpdateError(f"not installing {found[0]['tag']} now: {reason}")
            install(config, directory, user, found)
    except UpdateError as e:
        save_state(status="failed", message=str(e), error=str(e))
        sys.exit(1)
    except Exception as e:
        save_state(status="failed", message=f"unexpected error: {type(e).__name__}: {e}", error=str(e))
        raise


if __name__ == "__main__":
    main()
