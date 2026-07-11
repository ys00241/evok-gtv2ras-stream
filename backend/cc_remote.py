"""
Chromecast ADB Remote Control — Standalone Service
==================================================
獨立 module，唔需要可以唔 deploy。
等 streaming stack 行順咗先加呢 part。

Deploy: docker-compose.yml 加多個 service 就得
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Config ─────────────────────────────────────────────
CC_HOST = os.environ.get("CC_HOST", "")
ADB_PORT = os.environ.get("ADB_PORT", "5555")
STREAM_DIR = Path("/tmp/hls")

# ─── ADB Helpers ────────────────────────────────────────

def adb_cmd(args, timeout=5):
    """Run adb command with timeout."""
    if not CC_HOST:
        return False, "CC_HOST not configured"
    full_cmd = ["adb", "-s", f"{CC_HOST}:{ADB_PORT}"] + args
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "adb timeout"
    except FileNotFoundError:
        return False, "adb not found (try: apt install adb)"


def adb_connect():
    """Connect to Chromecast via ADB."""
    ok, out = adb_cmd(["connect", f"{CC_HOST}:{ADB_PORT}"], timeout=5)
    if ok and ("connected" in out or "already connected" in out):
        return True
    return False


# ─── API Routes ─────────────────────────────────────────

@app.route("/api/cc/connect", methods=["POST"])
def cc_connect():
    """Connect to Chromecast. Optionally set host via POST body."""
    data = request.get_json() or {}
    global CC_HOST
    if "host" in data:
        CC_HOST = data["host"]
    if not CC_HOST:
        return jsonify({"status": "error", "message": "No CC_HOST configured"}), 400
    ok = adb_connect()
    if ok:
        return jsonify({"status": "ok", "message": f"Connected to {CC_HOST}"})
    return jsonify({"status": "error", "message": f"Failed to connect to {CC_HOST}"}), 502


@app.route("/api/cc/status", methods=["GET"])
def cc_status():
    ok, out = adb_cmd(["get-state"])
    return jsonify({
        "status": "ok",
        "connected": ok,
        "host": CC_HOST,
        "device_state": out if ok else "disconnected",
    })


@app.route("/api/cc/nav/<key>", methods=["POST"])
def cc_nav(key):
    """D-pad navigation keys: up, down, left, right, ok, back, home, search, menu, power"""
    KEY_MAP = {
        "up": "KEYCODE_DPAD_UP",
        "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT",
        "ok": "KEYCODE_DPAD_CENTER",
        "center": "KEYCODE_DPAD_CENTER",
        "back": "KEYCODE_BACK",
        "home": "KEYCODE_HOME",
        "menu": "KEYCODE_MENU",
        "search": "KEYCODE_SEARCH",
        "power": "KEYCODE_POWER",
    }
    adb_key = KEY_MAP.get(key.lower())
    if not adb_key:
        return jsonify({"status": "error", "message": f"Unknown key: {key}"}), 400

    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key])
    return jsonify({"status": "ok" if ok else "error", "message": out})


@app.route("/api/cc/vol/<action>", methods=["POST"])
def cc_vol(action):
    """Volume control: up, down, mute"""
    KEY_MAP = {
        "up": "KEYCODE_VOLUME_UP",
        "down": "KEYCODE_VOLUME_DOWN",
        "mute": "KEYCODE_VOLUME_MUTE",
    }
    adb_key = KEY_MAP.get(action.lower())
    if not adb_key:
        return jsonify({"status": "error", "message": f"Unknown action: {action}"}), 400

    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key])
    return jsonify({"status": "ok" if ok else "error", "message": out})


@app.route("/api/cc/app/<name>", methods=["POST"])
def cc_launch_app(name):
    """Launch app by package name."""
    APPS = {
        "youtube": "com.google.android.youtube.tv",
        "netflix": "com.netflix.ninja",
        "disneyplus": "com.disney.disneyplus",
        "prime": "com.amazon.amazonvideo.livingroom",
        "spotify": "com.spotify.tv",
        "plex": "com.plexapp.android",
    }
    pkg = APPS.get(name.lower())
    if not pkg:
        return jsonify({"status": "error", "message": f"Unknown app: {name}"}), 400

    ok, out = adb_cmd(["shell", "monkey", "-p", pkg, "1"])
    return jsonify({
        "status": "ok" if ok else "error",
        "message": f"Launched {name}" if ok else out,
    })


@app.route("/api/cc/text", methods=["POST"])
def cc_text():
    """Send text input (e.g. search terms)."""
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"status": "error", "message": "No text"}), 400

    ok, out = adb_cmd(["shell", "input", "text", text])
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/screenshot", methods=["GET"])
def cc_screenshot():
    """Take screenshot of Chromecast screen and return PNG."""
    screenshot_path = STREAM_DIR / "cc_screen.png"
    ok, _ = adb_cmd(["shell", "screencap", "-p", "/sdcard/screen.png"])
    if not ok:
        return jsonify({"status": "error", "message": "screencap failed"}), 502
    ok, _ = adb_cmd(["pull", "/sdcard/screen.png", str(screenshot_path)])
    if not ok:
        return jsonify({"status": "error", "message": "pull failed"}), 502
    if screenshot_path.exists():
        return send_file(str(screenshot_path), mimetype="image/png")
    return jsonify({"status": "error", "message": "screenshot file not found"}), 500


# ─── Health ───

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "cc-remote"})


if __name__ == "__main__":
    if CC_HOST:
        print(f"[cc-remote] Auto-connecting to {CC_HOST}:{ADB_PORT}...")
        if adb_connect():
            print("[cc-remote] Connected!")
        else:
            print("[cc-remote] Connection failed. Will retry on API call.")
    app.run(host="0.0.0.0", port="5001", debug=False)
