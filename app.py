"""
TV-STREAM Backend — Flask API Server
Serves EVERYTHING: HLS + Web UI + Recording + API
Single container, no nginx dependency
"""

import os, signal, subprocess, threading, time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__, static_folder=None)
CORS(app)

# ─── Config ───
STREAM_DIR = Path(os.environ.get("STREAM_DIR", "/hls"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "/recordings"))
# Where the Dockerfile placed web/ files
BASE_DIR = Path(__file__).parent
WEB_UI_DIR = BASE_DIR / "web"
if not WEB_UI_DIR.exists():
    # Fallback for older image layout
    WEB_UI_DIR = Path("/usr/share/nginx/html")
    if not WEB_UI_DIR.exists():
        WEB_UI_DIR = BASE_DIR  # last resort

STREAM_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ───
ffmpeg_proc = None
record_proc = None

stream_config = {
    "resolution": "1280x720", "fps": 30, "bitrate": "4M",
    "hw_encoder": os.environ.get("HW_ENCODER", "libx264"),
}

# ─── Chromecast ADB Config ───
CC_HOST = os.environ.get("CC_HOST", "")
ADB_PORT = os.environ.get("ADB_PORT", "5555")
SCR_DIR = Path("/tmp/hls")
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
    "enabled": False, "quality": "same", "mode": "segment",
    "segment_seconds": 300, "destination": "local",
    "nas_path": "", "output_dir": str(RECORD_DIR),
}

# ─── ffmpeg helpers ───
def make_ffmpeg_cmd():
    cfg = stream_config
    # Audio device: first capture card ALSA device
    audio_device = os.environ.get("AUDIO_DEV", "hw:3,0")
    cmd = ["ffmpeg", "-y",
           "-f", "v4l2", "-input_format", "mjpeg",
           "-framerate", str(cfg["fps"]), "-video_size", cfg["resolution"],
           "-i", "/dev/video0",
           "-f", "alsa", "-i", audio_device,
           "-fflags", "nobuffer+low_delay", "-flags", "low_delay",
           "-c:v", cfg["hw_encoder"],
           "-b:v", cfg["bitrate"], "-maxrate", cfg["bitrate"],
           "-bufsize", f"{int(cfg['bitrate'].replace('M',''))}M",
           "-preset", "ultrafast", "-tune", "zerolatency",
           "-g", str(cfg["fps"]), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
           "-use_wallclock_as_timestamps", "1", "-flush_packets", "1"]
    active = [ch for ch, info in channels.items() if info["enabled"]]
    n = len(active)
    if n == 0:
        return None
    idx = 0
    if n > 1:
        splits = ",".join([f"[out_{i}]" for i in range(n)])
        cmd += ["-filter_complex", f"split={n}{splits}"]
    if channels["hls"]["enabled"]:
        if n > 1:
            cmd += ["-map", f"[out_{idx}]"]
            idx += 1
        cmd += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
                "-hls_flags", "delete_segments+omit_endlist",
                "-hls_segment_type", "mpegts", "-progress", "-",
                str(STREAM_DIR / "stream.m3u8")]
    if channels["teams"]["enabled"] and channels["teams"]["rtmp_url"]:
        if n > 1:
            cmd += ["-map", f"[out_{idx}]"]
            idx += 1
        cmd += ["-f", "flv", f"{channels['teams']['rtmp_url']}/{channels['teams']['rtmp_key']}"]
    if channels["telegram"]["enabled"] and channels["telegram"]["rtmp_url"]:
        if n > 1:
            cmd += ["-map", f"[out_{idx}]"]
            idx += 1
        cmd += ["-f", "flv", channels["telegram"]["rtmp_url"]]
    return cmd


def run_ffmpeg(cmd, tag="ffmpeg"):
    app.logger.info(f"[{tag}] {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=lambda: signal.signal(signal.SIGTERM, lambda s, f: None))


def stop_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass


# ═══════════════════════════════════════════
# ROUTES — specific BEFORE catch-all
# ═══════════════════════════════════════════

# ── Health ──
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "tv-stream-backend"})


# ── System ──
@app.route("/api/system/info")
def system_info():
    info = {"v4l2_detected": False, "devices": []}
    try:
        r = subprocess.run(["v4l2-ctl", "--list-devices"],
                           capture_output=True, text=True, timeout=3)
        info["v4l2_detected"] = "/dev/video0" in r.stdout
        info["devices"] = r.stdout
    except Exception:
        pass
    # List video devices
    try:
        r = subprocess.run("ls -la /dev/video* 2>/dev/null || echo 'no video devices'",
                           shell=True, capture_output=True, text=True, timeout=3)
        info["dev_list"] = r.stdout
    except Exception:
        pass
    return jsonify({"status": "ok", "info": info})


# ── Stream Control ──
@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    global ffmpeg_proc
    try:
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            return jsonify({"status": "already_running"})
        cmd = make_ffmpeg_cmd()
        if cmd is None:
            return jsonify({"status": "error", "message": "No channels enabled"}), 400
        app.logger.info(f"[stream] Starting ffmpeg: {' '.join(cmd)}")
        ffmpeg_proc = run_ffmpeg(cmd)
        time.sleep(1.5)
        if ffmpeg_proc.poll() is not None:
            out, err = ffmpeg_proc.communicate()
            err_text = (err or b"").decode("utf-8", errors="replace")[:2000]
            out_text = (out or b"").decode("utf-8", errors="replace")[:500]
            app.logger.error(f"[stream] ffmpeg died. exit={ffmpeg_proc.returncode}\nstderr:\n{err_text}\nstdout:\n{out_text}")
            return jsonify({"status": "error", "message": err_text}), 500
        return jsonify({"status": "ok"})
    except Exception as e:
        app.logger.exception(f"[stream] Unexpected error in stream_start: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    global ffmpeg_proc
    if ffmpeg_proc:
        stop_process(ffmpeg_proc)
        ffmpeg_proc = None
    # Clean up old HLS files
    for f in STREAM_DIR.glob("*"):
        try:
            f.unlink()
        except Exception:
            pass
    return jsonify({"status": "ok"})


@app.route("/api/stream/restart", methods=["POST"])
def stream_restart():
    stream_stop()
    time.sleep(0.5)
    return stream_start()


@app.route("/api/stream/config", methods=["GET", "PUT"])
def stream_config_ep():
    global stream_config
    if request.method == "GET":
        cp = "custom"
        for n, p in RES_PRESETS.items():
            if p["resolution"] == stream_config["resolution"] and p["fps"] == stream_config["fps"]:
                cp = n
                break
        return jsonify({
            "status": "ok", "config": stream_config,
            "presets": list(RES_PRESETS.keys()), "current_preset": cp
        })
    data = request.get_json(silent=True) or {}
    if "preset" in data and data["preset"] in RES_PRESETS:
        stream_config.update(RES_PRESETS[data["preset"]])
    else:
        for k in ("resolution", "fps", "bitrate"):
            if k in data:
                stream_config[k] = data[k]
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
    return jsonify({"status": "ok", "config": stream_config})


@app.route("/api/stream/status", methods=["GET"])
def stream_status():
    running = ffmpeg_proc is not None and ffmpeg_proc.poll() is None
    hls_ready = (STREAM_DIR / "stream.m3u8").exists()
    cp = "custom"
    for n, p in RES_PRESETS.items():
        if p["resolution"] == stream_config["resolution"] and p["fps"] == stream_config["fps"]:
            cp = n
            break
    return jsonify({
        "status": "ok", "running": running, "hls_ready": hls_ready,
        "config": stream_config,
        "channels": {k: v["enabled"] for k, v in channels.items()},
        "current_preset": cp
    })


# ── Channels ──
@app.route("/api/channel/status", methods=["GET"])
def channel_status():
    return jsonify({"status": "ok", "channels": channels})


@app.route("/api/channel/<name>", methods=["GET", "PUT"])
def channel_control(name):
    if name not in channels:
        return jsonify({"status": "error", "message": f"Unknown channel: {name}"}), 404
    if request.method == "GET":
        return jsonify({"status": "ok", "config": channels[name]})
    data = request.get_json(silent=True) or {}
    for k in ("enabled", "rtmp_url", "rtmp_key"):
        if k in data:
            channels[name][k] = data[k]
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
    return jsonify({"status": "ok", "config": channels[name]})


# ── Recording ──
@app.route("/api/record/start", methods=["POST"])
def record_start():
    global record_proc
    if record_proc and record_proc.poll() is None:
        return jsonify({"status": "error", "message": "Already recording"}), 400

    data = request.get_json(silent=True) or {}
    rc = {**record_config}
    for k in ("quality", "mode", "segment_seconds", "destination"):
        if k in data:
            rc[k] = data[k]

    q = rc["quality"]
    if q == "same":
        res = stream_config["resolution"]
        fps = stream_config["fps"]
    else:
        res = "1280x720" if q == "720p" else "1920x1080"
        fps = 30

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    od = Path(rc["output_dir"])
    od.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-f", "v4l2", "-input_format", "mjpeg",
           "-framerate", str(fps), "-video_size", res,
           "-i", "/dev/video0", "-c:v", "h264_v4l2m2m",
           "-b:v", "4M", "-preset", "ultrafast",
           "-use_wallclock_as_timestamps", "1"]

    if rc["mode"] == "segment":
        cmd += ["-f", "segment", "-segment_time", str(rc["segment_seconds"]),
                "-reset_timestamps", "1", "-strftime", "1",
                str(od / f"capture_{now}_%03d.mp4")]
    else:
        cmd += [str(od / f"capture_{now}.mp4")]

    record_proc = run_ffmpeg(cmd, "record")
    time.sleep(0.8)
    if record_proc and record_proc.poll() is not None:
        _, err = record_proc.communicate()
        return jsonify({"status": "error", "message": err[:500]}), 500
    return jsonify({"status": "ok"})


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    global record_proc
    if record_proc:
        stop_process(record_proc)
        record_proc = None
    return jsonify({"status": "ok"})


@app.route("/api/record/status", methods=["GET"])
def record_status():
    running = record_proc is not None and record_proc.poll() is None
    files = sorted(RECORD_DIR.glob("*.mp4"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    total_mb = sum(f.stat().st_size for f in files) / 1048576 if files else 0
    return jsonify({
        "status": "ok", "running": running,
        "files": [{"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1)}
                  for f in files[:20]],
        "disk_used_mb": total_mb
    })


@app.route("/api/record/files/<path:filename>")
def record_download(filename):
    fp = RECORD_DIR / filename
    if not fp.exists():
        return jsonify({"status": "error", "message": "File not found"}), 404
    return send_file(str(fp), mimetype="video/mp4")


# ── HLS segments (serve from STREAM_DIR) ──
@app.route("/hls/<path:filename>")
def serve_hls(filename):
    fp = STREAM_DIR / filename
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    ct = "video/mp2t"
    if filename.endswith(".m3u8"):
        ct = "application/vnd.apple.mpegurl"
    return send_file(str(fp), mimetype=ct)


# ── Recorded files ──
@app.route("/recordings/<path:filename>")
def serve_recording(filename):
    fp = RECORD_DIR / filename
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(fp), mimetype="video/mp4")


# ═══════════════════════════════════════════
# Chromecast ADB Remote Control
# ═══════════════════════════════════════════

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
        return False, "adb not found"


@app.route("/api/cc/connect", methods=["POST"])
def cc_connect():
    global CC_HOST
    data = request.get_json(silent=True) or {}
    if "host" in data:
        CC_HOST = data["host"]
    if not CC_HOST:
        return jsonify({"status": "error", "message": "No CC_HOST configured"}), 400
    ok, out = adb_cmd(["connect", f"{CC_HOST}:{ADB_PORT}"], timeout=5)
    if ok and ("connected" in out or "already" in out):
        return jsonify({"status": "ok", "message": f"Connected to {CC_HOST}"})
    return jsonify({"status": "error", "message": f"Failed to connect: {out}"}), 502


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
    KEY_MAP = {
        "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
        "ok": "KEYCODE_DPAD_CENTER", "center": "KEYCODE_DPAD_CENTER",
        "back": "KEYCODE_BACK", "home": "KEYCODE_HOME",
        "menu": "KEYCODE_MENU", "search": "KEYCODE_SEARCH", "power": "KEYCODE_POWER",
    }
    adb_key = KEY_MAP.get(key.lower())
    if not adb_key:
        return jsonify({"status": "error", "message": f"Unknown key: {key}"}), 400
    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key])
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/vol/<action>", methods=["POST"])
def cc_vol(action):
    KEY_MAP = {"up": "KEYCODE_VOLUME_UP", "down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE"}
    adb_key = KEY_MAP.get(action.lower())
    if not adb_key:
        return jsonify({"status": "error", "message": f"Unknown action: {action}"}), 400
    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key])
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/app/<name>", methods=["POST"])
def cc_launch_app(name):
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
    return jsonify({"status": "ok" if ok else "error", "message": f"Launched {name}" if ok else out})


@app.route("/api/cc/text", methods=["POST"])
def cc_text():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"status": "error", "message": "No text"}), 400
    ok, out = adb_cmd(["shell", "input", "text", text])
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/screenshot", methods=["GET"])
def cc_screenshot():
    screenshot_path = SCR_DIR / "cc_screen.png"
    ok, _ = adb_cmd(["shell", "screencap", "-p", "/sdcard/screen.png"])
    if not ok:
        return jsonify({"status": "error", "message": "screencap failed"}), 502
    ok, _ = adb_cmd(["pull", "/sdcard/screen.png", str(screenshot_path)])
    if not ok:
        return jsonify({"status": "error", "message": "pull failed"}), 502
    if screenshot_path.exists():
        return send_file(str(screenshot_path), mimetype="image/png")
    return jsonify({"status": "error", "message": "screenshot not found"}), 500


# ── Web UI (catch-all — MUST be last) ──
@app.route("/")
def serve_index():
    return send_from_directory(str(WEB_UI_DIR), "index.html")


@app.route("/<path:filename>")
def serve_ui(filename):
    """Serve static files (JS, CSS, assets), fallback to index.html for SPA."""
    if not filename:
        return send_from_directory(str(WEB_UI_DIR), "index.html")
    fp = WEB_UI_DIR / filename
    # Only serve files that actually exist as static assets
    if fp.exists() and fp.is_file():
        # Map extensions to MIME types
        ext = fp.suffix.lower()
        if ext in ('.html', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif',
                   '.svg', '.ico', '.webp', '.woff', '.woff2', '.json',
                   '.map', '.txt'):
            return send_from_directory(str(WEB_UI_DIR), filename)
    # SPA fallback — everything else returns index.html
    return send_from_directory(str(WEB_UI_DIR), "index.html")


if __name__ == "__main__":
    app.logger.info(f"TV-STREAM Backend starting on port {PORT}")
    app.logger.info(f"WEB_UI_DIR={WEB_UI_DIR}")
    app.logger.info(f"Web files exist: {list(WEB_UI_DIR.glob('*')) if WEB_UI_DIR.exists() else 'DIR NOT FOUND'}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
