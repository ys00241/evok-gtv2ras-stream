"""
TV-STREAM Backend — Flask API Server
====================================
Stream Manager + Recording Control
(Chromecast ADB Remote in separate module: cc_remote.py)
"""

import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Config ─────────────────────────────────────────────
STREAM_DIR = Path(os.environ.get("STREAM_DIR", "/tmp/hls"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "/recordings"))

STREAM_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ──────────────────────────────────────────────
ffmpeg_proc = None  # live streaming ffmpeg
record_proc = None  # recording ffmpeg (separate)

stream_config = {
    "resolution": "1920x1080",
    "fps": 30,
    "bitrate": "6M",
    "hw_encoder": "h264_v4l2m2m",
}
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
    "quality": "same",
    "mode": "segment",
    "segment_seconds": 300,
    "destination": "local",
    "nas_path": "",
    "output_dir": str(RECORD_DIR),
}

# ─── Helpers ────────────────────────────────────────────

def make_ffmpeg_cmd():
    """Build ffmpeg command for live streaming with active channels."""
    cfg = stream_config
    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", str(cfg["fps"]),
        "-video_size", cfg["resolution"],
        "-i", "/dev/video0",
        "-c:v", cfg["hw_encoder"],
        "-b:v", cfg["bitrate"],
        "-maxrate", cfg["bitrate"],
        "-bufsize", f"{int(cfg['bitrate'].replace('M',''))*2}M",
        "-preset", "ultrafast",
        "-g", str(cfg["fps"]),
        "-use_wallclock_as_timestamps", "1",
        "-flush_packets", "1",
        "-fps_mode", "cfr",
    ]

    active = [ch for ch, info in channels.items() if info["enabled"]]
    n_active = len(active)
    if n_active == 0:
        return None

    # Build filter_complex split for multiple outputs
    if n_active > 1:
        splits = ",".join([f"[out_{i}]" for i in range(n_active)])
        cmd += ["-filter_complex", f"split={n_active}{splits}"]

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
        cmd += [
            "-f", "segment",
            "-segment_time", str(config["segment_seconds"]),
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(out_dir / f"capture_{now}_%03d.mp4"),
        ]
    else:
        cmd += [str(out_dir / f"capture_{now}.mp4")]

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

    if "preset" in data and data["preset"] in RES_PRESETS:
        stream_config.update(RES_PRESETS[data["preset"]])
    else:
        if "resolution" in data:
            stream_config["resolution"] = data["resolution"]
        if "fps" in data:
            stream_config["fps"] = int(data["fps"])
        if "bitrate" in data:
            stream_config["bitrate"] = data["bitrate"]

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
    return jsonify({"status": "ok", "channels": channels})


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

    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
        msg = "Channel updated, stream restarting"
    else:
        msg = "Channel updated"

    return jsonify({"status": "ok", "message": msg, "channel": name, "config": channels[name]})


# --- Recording ---

@app.route("/api/record/start", methods=["POST"])
def record_start():
    global record_proc
    if record_proc and record_proc.poll() is None:
        return jsonify({"status": "error", "message": "Recording already active"}), 400

    data = request.get_json() or {}
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

    return jsonify({"status": "ok", "message": "Recording started"})


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
    import platform
    info = {
        "hostname": platform.node(),
        "python": platform.python_version(),
    }
    try:
        r = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True, timeout=3)
        info["v4l2_detected"] = "/dev/video0" in r.stdout
    except Exception:
        info["v4l2_detected"] = False
    try:
        r = subprocess.run(["cat", "/proc/uptime"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            s = float(r.stdout.split()[0])
            info["uptime"] = f"{int(s // 3600)}h {int((s % 3600) // 60)}m"
    except Exception:
        info["uptime"] = "unknown"
    return jsonify({"status": "ok", "info": info})


# --- Health ---

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-stream-backend"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
