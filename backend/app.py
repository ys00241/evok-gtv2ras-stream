"""
TV-STREAM Backend — Flask API Server
====================================
Stream Manager + Chromecast ADB Remote + Recording Control
"""

import os
import json
import signal
import subprocess
import threading
import time
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Config ─────────────────────────────────────────────
STREAM_DIR = Path(os.environ.get("STREAM_DIR", "/tmp/hls"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "/recordings"))
CC_HOST = os.environ.get("CC_HOST", "192.168.0.18")
ADB_PORT = os.environ.get("ADB_PORT", "5555")

STREAM_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ──────────────────────────────────────────────
ffmpeg_proc = None  # live streaming ffmpeg
record_proc = None  # recording ffmpeg (separate)

stream_config = {
    "resolution": "1920x1080",    # or 1280x720
    "fps": 30,                    # or 60
    "bitrate": "5M",
    "hw_encoder": "h264_v4l2m2m",
}
# resolution template
RES_PRESETS = {
    "720p@60": {"resolution": "1280x720", "fps": 60, "bitrate": "4M"},
    "1080p@30": {"resolution": "1920x1080", "fps": 30, "bitrate": "6M"},
}

channels = {
    "hls": {"enabled": True, "name": "HLS"},
    "teams": {"enabled": False, "name": "Microsoft Teams", "rtmp_url": "", "rtmp_key": ""},
    "telegram": {"enabled": False, "name": "Telegram", "rtmp_url": ""},
}

record_config = {
    "enabled": False,
    "quality": "same",         # same / 720p / 1080p
    "mode": "segment",         # single / segment
    "segment_seconds": 300,    # 5 min
    "destination": "local",    # local / nas / gdrive
    "nas_path": "",
    "output_dir": str(RECORD_DIR),
}

cc_connected = False

# ─── Helpers ────────────────────────────────────────────

def make_ffmpeg_cmd():
    """Build ffmpeg command for live streaming with active channels."""
    cfg = stream_config
    res = cfg["resolution"]
    fps = cfg["fps"]
    br = cfg["bitrate"]
    enc = cfg["hw_encoder"]

    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", str(fps),
        "-video_size", res,
        "-i", "/dev/video0",
        "-c:v", enc,
        "-b:v", br,
        "-maxrate", br,
        "-bufsize", f"{int(br.replace('M',''))*2}M",
        "-preset", "ultrafast",
        "-g", str(fps),
        "-use_wallclock_as_timestamps", "1",
        "-flush_packets", "1",
        "-fps_mode", "cfr",
    ]

    # Determine number of active outputs for split
    active = [ch for ch, info in channels.items() if info["enabled"]]
    n_active = len(active)
    if n_active == 0:
        return None  # nothing to stream

    # Build filter_complex split
    if n_active > 1:
        splits = ",".join([f"[out_{i}]" for i in range(n_active)])
        cmd += ["-filter_complex", f"split={n_active}{splits}"]

    # Output mapping
    out_idx = 0
    outputs = []
    
    if channels["hls"]["enabled"]:
        hls_path = str(STREAM_DIR / "stream.m3u8")
        if n_active > 1:
            outputs += ["-map", f"[out_{out_idx}]"]
            out_idx += 1
        outputs += [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+omit_endlist",
            "-hls_segment_type", "mpegts",
            "-progress", "-",
            hls_path,
        ]

    if channels["teams"]["enabled"] and channels["teams"]["rtmp_url"]:
        if n_active > 1:
            outputs += ["-map", f"[out_{out_idx}]"]
            out_idx += 1
        rtmp_full = f"{channels['teams']['rtmp_url']}/{channels['teams']['rtmp_key']}"
        outputs += ["-f", "flv", rtmp_full]

    if channels["telegram"]["enabled"] and channels["telegram"]["rtmp_url"]:
        if n_active > 1:
            outputs += ["-map", f"[out_{out_idx}]"]
            out_idx += 1
        outputs += ["-f", "flv", channels["telegram"]["rtmp_url"]]

    cmd += outputs
    return cmd


def make_record_cmd(config=None):
    """Build ffmpeg command for recording (separate process)."""
    if config is None:
        config = record_config

    quality = config["quality"]
    if quality == "same":
        res = stream_config["resolution"]
        fps = stream_config["fps"]
    elif quality == "720p":
        res = "1280x720"
        fps = 30
    else:  # 1080p
        res = "1920x1080"
        fps = 30

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(config["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", str(fps),
        "-video_size", res,
        "-i", "/dev/video0",
        "-c:v", "h264_v4l2m2m",
        "-b:v", "4M",
        "-preset", "ultrafast",
        "-use_wallclock_as_timestamps", "1",
    ]

    if config["mode"] == "segment":
        seg = config["segment_seconds"]
        cmd += [
            "-f", "segment",
            "-segment_time", str(seg),
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(out_dir / f"capture_{now}_%03d.mp4"),
        ]
    else:
        cmd += [
            str(out_dir / f"capture_{now}.mp4"),
        ]

    return cmd


def run_ffmpeg(cmd, tag="ffmpeg"):
    """Start ffmpeg as subprocess, return Popen."""
    app.logger.info(f"[{tag}] {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=lambda: signal.signal(signal.SIGTERM, lambda s, f: None),
    )


def stop_process(proc, tag="process"):
    """Gracefully stop a subprocess."""
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except Exception:
        pass


# ─── ADB Helpers ────────────────────────────────────────

def adb_cmd(args, timeout=5):
    """Run adb command with timeout."""
    full_cmd = ["adb", "-s", f"{CC_HOST}:{ADB_PORT}"] + args
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "adb timeout"
    except FileNotFoundError:
        return False, "adb not found"


def adb_connect():
    """Connect to Chromecast via ADB."""
    global cc_connected
    ok, out = adb_cmd(["connect", f"{CC_HOST}:{ADB_PORT}"], timeout=5)
    if ok and ("connected" in out or "already connected" in out):
        cc_connected = True
        return True
    cc_connected = False
    return False


# ─── API Routes ─────────────────────────────────────────

# --- Stream Management ---

@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    global ffmpeg_proc
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        return jsonify({"status": "already_running", "message": "Stream already active"})

    cmd = make_ffmpeg_cmd()
    if cmd is None:
        return jsonify({"status": "error", "message": "No channels enabled"}), 400

    ffmpeg_proc = run_ffmpeg(cmd)
    time.sleep(0.5)
    if ffmpeg_proc.poll() is not None:
        _, err = ffmpeg_proc.communicate()
        return jsonify({"status": "error", "message": f"ffmpeg died: {err[:200]}"}), 500

    return jsonify({"status": "ok", "message": "Stream started"})


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    global ffmpeg_proc
    if ffmpeg_proc:
        stop_process(ffmpeg_proc, "ffmpeg")
        ffmpeg_proc = None
        return jsonify({"status": "ok", "message": "Stream stopped"})
    return jsonify({"status": "ok", "message": "No stream running"})


@app.route("/api/stream/restart", methods=["POST"])
def stream_restart():
    stream_stop()
    time.sleep(0.5)
    return stream_start()


@app.route("/api/stream/config", methods=["GET", "PUT"])
def stream_config_ep():
    if request.method == "GET":
        return jsonify({
            "status": "ok",
            "config": stream_config,
            "presets": list(RES_PRESETS.keys()),
            "current_preset": get_current_preset(),
        })
    
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No config data"}), 400

    # Check for preset
    if "preset" in data and data["preset"] in RES_PRESETS:
        stream_config.update(RES_PRESETS[data["preset"]])
    else:
        if "resolution" in data:
            stream_config["resolution"] = data["resolution"]
        if "fps" in data:
            stream_config["fps"] = int(data["fps"])
        if "bitrate" in data:
            stream_config["bitrate"] = data["bitrate"]

    # Restart stream if running
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
        return jsonify({"status": "ok", "message": "Config updated, stream restarting"})

    return jsonify({"status": "ok", "config": stream_config})


def get_current_preset():
    for name, preset in RES_PRESETS.items():
        if (preset["resolution"] == stream_config["resolution"] 
            and preset["fps"] == stream_config["fps"]):
            return name
    return "custom"


@app.route("/api/stream/status", methods=["GET"])
def stream_status():
    running = ffmpeg_proc is not None and ffmpeg_proc.poll() is None
    # Check if HLS file exists
    hls_exists = (STREAM_DIR / "stream.m3u8").exists()
    return jsonify({
        "status": "ok",
        "running": running,
        "hls_ready": hls_exists,
        "config": stream_config,
        "current_preset": get_current_preset(),
        "channels": {k: v["enabled"] for k, v in channels.items()},
    })


# --- Channels ---

@app.route("/api/channel/status", methods=["GET"])
def channel_status():
    return jsonify({
        "status": "ok",
        "channels": channels,
    })


@app.route("/api/channel/<name>", methods=["GET", "PUT"])
def channel_control(name):
    if name not in channels:
        return jsonify({"status": "error", "message": f"Unknown channel: {name}"}), 404

    if request.method == "GET":
        return jsonify({"status": "ok", "channel": name, "config": channels[name]})

    data = request.get_json()
    if data is None:
        return jsonify({"status": "error", "message": "No data"}), 400

    if "enabled" in data:
        channels[name]["enabled"] = bool(data["enabled"])
    if "rtmp_url" in data:
        channels[name]["rtmp_url"] = data["rtmp_url"]
    if "rtmp_key" in data and name == "teams":
        channels[name]["rtmp_key"] = data["rtmp_key"]

    # Restart stream if running
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
        msg = "Channel updated, stream restarting"
    else:
        msg = "Channel updated"

    return jsonify({"status": "ok", "message": msg, "channel": name, "config": channels[name]})


# --- Chromecast Remote ---

@app.route("/api/cc/connect", methods=["POST"])
def cc_connect():
    data = request.get_json() or {}
    global CC_HOST
    if "host" in data:
        CC_HOST = data["host"]
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
    """D-pad navigation. Keys: up, down, left, right, ok, back, home, search, menu"""
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
    if action == "up":
        ok, out = adb_cmd(["shell", "input", "keyevent", "KEYCODE_VOLUME_UP"])
    elif action == "down":
        ok, out = adb_cmd(["shell", "input", "keyevent", "KEYCODE_VOLUME_DOWN"])
    elif action == "mute":
        ok, out = adb_cmd(["shell", "input", "keyevent", "KEYCODE_VOLUME_MUTE"])
    elif action == "set":
        data = request.get_json() or {}
        level = data.get("level", 50)
        # ADB can't set volume directly easily; use repeated press
        return jsonify({"status": "error", "message": "Set volume not supported via ADB; use up/down"}), 400
    else:
        return jsonify({"status": "error", "message": f"Unknown action: {action}"}), 400
    
    return jsonify({"status": "ok" if ok else "error", "message": out})


@app.route("/api/cc/app/<name>", methods=["POST"])
def cc_launch_app(name):
    """Launch an app by package name shortcut."""
    APPS = {
        "youtube": "com.google.android.youtube.tv",
        "netflix": "com.netflix.ninja",
        "disney+": "com.disney.disneyplus",
        "disneyplus": "com.disney.disneyplus",
        "prime": "com.amazon.amazonvideo.livingroom",
        "primevideo": "com.amazon.amazonvideo.livingroom",
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
    """Type text (for search)."""
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"status": "error", "message": "No text"}), 400
    # Escape special chars for adb shell
    escaped = text.replace(" ", "%s").replace("&", "\\&")
    ok, out = adb_cmd(["shell", "input", "text", text])
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/screenshot", methods=["GET"])
def cc_screenshot():
    """Take screenshot of Chromecast screen."""
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


# --- Recording ---

@app.route("/api/record/start", methods=["POST"])
def record_start():
    global record_proc
    if record_proc and record_proc.poll() is None:
        return jsonify({"status": "error", "message": "Recording already active"}), 400

    data = request.get_json() or {}
    # Apply any overrides from request
    rc = {**record_config}
    for key in ["quality", "mode", "segment_seconds", "destination"]:
        if key in data:
            rc[key] = data[key]

    cmd = make_record_cmd(rc)
    record_proc = run_ffmpeg(cmd, tag="record")
    time.sleep(0.5)
    if record_proc.poll() is not None:
        _, err = record_proc.communicate()
        return jsonify({"status": "error", "message": f"Record ffmpeg died: {err[:200]}"}), 500

    return jsonify({
        "status": "ok",
        "message": "Recording started",
        "config": rc,
        "output_dir": rc["output_dir"],
    })


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    global record_proc
    if record_proc:
        stop_process(record_proc, "record")
        record_proc = None
        return jsonify({"status": "ok", "message": "Recording stopped"})
    return jsonify({"status": "ok", "message": "No recording active"})


@app.route("/api/record/config", methods=["GET", "PUT"])
def record_config_ep():
    global record_config
    if request.method == "GET":
        return jsonify({"status": "ok", "config": record_config})

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data"}), 400

    for key in ["quality", "mode", "destination", "nas_path", "output_dir"]:
        if key in data:
            record_config[key] = data[key]
    if "segment_seconds" in data:
        record_config["segment_seconds"] = int(data["segment_seconds"])

    return jsonify({"status": "ok", "config": record_config})


@app.route("/api/record/status", methods=["GET"])
def record_status():
    running = record_proc is not None and record_proc.poll() is None
    files = sorted(RECORD_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    file_list = [{
        "name": f.name,
        "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
        "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
    } for f in files[:20]]

    return jsonify({
        "status": "ok",
        "running": running,
        "config": record_config,
        "files": file_list,
        "disk_used_mb": sum(f.stat().st_size for f in files) / (1024 * 1024) if files else 0,
    })


@app.route("/api/record/files/<filename>", methods=["GET"])
def record_download(filename):
    filepath = RECORD_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return jsonify({"status": "error", "message": "File not found"}), 404
    return send_file(str(filepath), as_attachment=True)


# --- System Info ---

@app.route("/api/system/info", methods=["GET"])
def system_info():
    """Basic system health info."""
    import platform
    info = {
        "hostname": platform.node(),
        "python": platform.python_version(),
        "uptime": "unknown",
    }
    try:
        r = subprocess.run(["cat", "/proc/uptime"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            uptime_secs = float(r.stdout.split()[0])
            info["uptime"] = f"{int(uptime_secs // 3600)}h {int((uptime_secs % 3600) // 60)}m"
    except Exception:
        pass
    # Check v4l2 device
    v4l2_ok = False
    try:
        r = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True, timeout=3)
        v4l2_ok = "/dev/video0" in r.stdout
    except Exception:
        pass
    info["v4l2_detected"] = v4l2_ok
    return jsonify({"status": "ok", "info": info})


# ─── Startup ────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-stream-backend"})


if __name__ == "__main__":
    # Try ADB connect on startup
    threading.Thread(target=lambda: adb_connect() if CC_HOST else None, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
