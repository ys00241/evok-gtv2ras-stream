"""
TV-STREAM Backend — Flask API Server
Serves: HLS + Web UI + Recording + API
Single container, no nginx dependency
"""
import os, signal, subprocess, threading, time, re
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory, Response
from flask_cors import CORS

# ─── Constants ───
PORT = int(os.environ.get("PORT", 5000))
WATCHDOG_INTERVAL = 3       # seconds between watchdog checks (faster detection)
WATCHDOG_MAX_RESTARTS = 5    # max auto-restarts before giving up
WATCHDOG_COOLDOWN = 10       # seconds after max restarts before retrying (faster recovery)
app = Flask(__name__, static_folder=None)
CORS(app)

# ─── Config ───
STREAM_DIR = Path(os.environ.get("STREAM_DIR", "/hls"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "/recordings"))
BASE_DIR = Path(__file__).parent
WEB_UI_DIR = BASE_DIR / "web"
if not WEB_UI_DIR.exists():
    WEB_UI_DIR = Path("/usr/share/nginx/html")
    if not WEB_UI_DIR.exists():
        WEB_UI_DIR = BASE_DIR

STREAM_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ───
ffmpeg_proc = None
record_proc = None

stream_config = {
    "resolution": "1920x1080", "fps": 30, "bitrate": "6M",
    "hw_encoder": os.environ.get("HW_ENCODER", "libx264"),
    "video_dev": os.environ.get("VIDEO_DEV", "/dev/video0"),
    "hls_time": int(os.environ.get("HLS_TIME", "1")),  # 1s segments for low latency
}

# ─── Chromecast ADB Config ───
CC_HOST = os.environ.get("CC_HOST", "")
ADB_PORT = os.environ.get("ADB_PORT", "5555")
SCR_DIR = Path("/tmp/hls")
RES_PRESETS = {
    "720p@30": {"resolution": "1280x720", "fps": 30, "bitrate": "4M"},
    "720p@60": {"resolution": "1280x720", "fps": 60, "bitrate": "6M"},
    "1080p@30": {"resolution": "1920x1080", "fps": 30, "bitrate": "6M"},
    "1080p@60": {"resolution": "1920x1080", "fps": 60, "bitrate": "12M"},
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

# ─── Audio auto-detect ───
def detect_audio_device():
    """Detect audio capture device from MS2130/MS2109 capture card.
    Returns None if no device found — stream will be video-only."""
    # 1. Check env override (for testing)
    env_dev = os.environ.get("AUDIO_DEV", "")
    if env_dev:
        try:
            r = subprocess.run(
                ["arecord", "-D", env_dev, "-d", "1", "-f", "S16_LE", "-r", "48000", "-c", "2", "/dev/null"],
                capture_output=True, timeout=3)
            if r.returncode == 0:
                app.logger.info(f"[audio] Using override: {env_dev}")
                return env_dev
        except Exception as e:
            app.logger.warning(f"[audio] Override {env_dev} failed: {e}")

    # 2. Auto-detect from arecord -l
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=2)
        app.logger.info(f"[audio] arecord -l output:\n{r.stdout}")
        for line in r.stdout.split("\n"):
            if "MS2109" in line or "MS2130" in line or "USB Audio" in line or "USB" in line:
                m = re.search(r"card (\d+):", line)
                if m:
                    dev = f"hw:{m.group(1)},0"
                    app.logger.info(f"[audio] Auto-detected: {dev}")
                    return dev
    except Exception as e:
        app.logger.warning(f"[audio] arecord -l failed: {e}")

    # 3. Try hw:3,0 as default for RPi MS2130 (common position)
    try:
        r = subprocess.run(
            ["arecord", "-D", "hw:3,0", "-d", "1", "-f", "S16_LE", "-r", "48000", "-c", "2", "/dev/null"],
            capture_output=True, timeout=3)
        if r.returncode == 0:
            app.logger.info("[audio] Using default hw:3,0")
            return "hw:3,0"
    except Exception:
        pass

    # 4. List all cards via cat /proc/asound/cards
    try:
        r = subprocess.run(["cat", "/proc/asound/cards"], capture_output=True, text=True, timeout=2)
        app.logger.info(f"[audio] /proc/asound/cards:\n{r.stdout}")
        # Look for USB audio devices
        for line in r.stdout.split("\n"):
            if "USB" in line or "MS21" in line:
                m = re.search(r"\s+(\d+)", line)
                if m:
                    dev = f"hw:{m.group(1)},0"
                    app.logger.info(f"[audio] Detected USB audio card: {dev}")
                    return dev
    except Exception:
        pass

    app.logger.warning("[audio] No audio device found — streaming video-only")
    return None


# ─── ffmpeg helpers ───
def encoder_flags(encoder, bitrate, low_latency=True, filter_already_applied=False):
    """Return ffmpeg encoder flags compatible with given encoder.
    - libx264: software, supports -preset -tune -bf -g
    - h264_v4l2m2m: RPi hardware, limited flag support
    - filter_already_applied: set True when format conversion done in filter_complex
    - encoder_dev: explicit V4L2 M2M device path (e.g. /dev/video11 on RPi)
    """
    flags = ["-c:v", encoder, "-b:v", bitrate]
    if encoder == "libx264":
        if low_latency:
            # Software encoder supports full low-latency flags
            flags += ["-bf", "0", "-g", "30", "-keyint_min", "30",
                      "-preset", "ultrafast", "-tune", "zerolatency",
                      "-pix_fmt", "yuv420p"]
        else:
            flags += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        # v4l2m2m (bcm2835-codec) — hardware encoder, limited flag support
        # DO NOT use -g (GOP) — bcm2835-codec rejects it → "Failed to set gop size: Invalid argument"
        # DO NOT use -use_wallclock_as_timestamps — corrupts H.264 bitstream
        # Add -bf 0 to disable B-frames (v4l2m2m has PTS issues with B-frames)
        flags = ["-c:v", encoder, "-b:v", bitrate, "-pix_fmt", "nv12", "-flush_packets", "1", "-bf", "0"]
    return flags

def make_ffmpeg_cmd():
    """Single ffmpeg command for HLS output only.
    /dev/video0 can only be opened once — everything must be in one process."""
    cfg = stream_config
    audio_device = detect_audio_device()
    # Auto-detect input format and FPS from capture device
    input_fmt = None
    actual_fps = cfg["fps"]
    video_size = cfg["resolution"]
    try:
        r = subprocess.run(["ffmpeg", "-y", "-t", "1",
                           "-f", "v4l2", "-i", cfg["video_dev"],
                           "-f", "null", "-"],
                          capture_output=True, text=True, timeout=5)
        app.logger.info(f"[input_detect] stderr: {r.stderr[:800]}")
        if "rawvideo" in r.stderr:
            input_fmt = None
        elif "mjpeg" in r.stderr:
            input_fmt = "mjpeg"
        import re
        fps_match = re.search(r"frame=\d+\s+fps=\s*(\d+\.?\d*)", r.stderr)
        if fps_match:
            detected_fps = float(fps_match.group(1))
            if detected_fps < actual_fps:
                actual_fps = int(detected_fps)
                app.logger.info(f"[input_detect] Detected FPS {detected_fps}, using {actual_fps}")
        # Extract actual input resolution from ffmpeg probe output
        # Format: "Stream #0:0: Video: ... WxH ..." or "Input #0, v4l2: ... WxH ..."
        res_match = re.search(r"(\d+)x(\d+)", r.stderr)
        if res_match:
            detected_w, detected_h = int(res_match.group(1)), int(res_match.group(2))
            config_w, config_h = map(int, video_size.split("x"))
            if (detected_w, detected_h) != (config_w, config_h):
                app.logger.warning(f"[input_detect] Input is {detected_w}x{detected_h}, config says {config_w}x{config_h}. Using native input resolution.")
                video_size = f"{detected_w}x{detected_h}"
        app.logger.info(f"[input_detect] Using input_fmt={input_fmt}, fps={actual_fps}, resolution={video_size}")
    except Exception as e:
        app.logger.warning(f"[input_detect] Failed: {e}")
    # ── Step 1: collect ALL inputs FIRST (global options must come after all -i) ──
    cmd = ["ffmpeg", "-y",
           "-f", "v4l2",
           "-thread_queue_size", os.environ.get("THREAD_QUEUE_VIDEO", "2048"),
           "-framerate", str(actual_fps),
           "-err_detect", "ignore_err",  # Skip corrupted V4L2 frames
           "-i", cfg["video_dev"]]
    if audio_device:
        cmd += ["-thread_queue_size", os.environ.get("THREAD_QUEUE_AUDIO", "1024"), "-f", "alsa", "-i", audio_device]

    # ── Step 2: Scale to target resolution while preserving aspect ratio ──
    # Use filter_complex for multi-input commands (video + audio)
    target_w, target_h = map(int, cfg["resolution"].split("x"))
    cmd += [
        "-filter_complex", f"[0:v]scale={target_w}:{target_h}:flags=bilinear[vid]",
        "-map", "[vid]",
        "-map", "1:a",
    ]

    active = [ch for ch, info in channels.items() if info["enabled"]]
    n = len(active)

    if n == 0:
        return None

    has_hls = channels["hls"]["enabled"]

    # ── HLS only (simplified — no dual output) ──
    if has_hls:
        cmd += encoder_flags(cfg["hw_encoder"], cfg["bitrate"], low_latency=True)
        if audio_device:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        # Output options AFTER encoder flags
        cmd += ["-muxdelay", "0",
                "-avoid_negative_ts", "make_zero",
                "-f", "hls", "-hls_time", str(stream_config["hls_time"]), "-hls_list_size", os.environ.get("HLS_LIST_SIZE", "10"),
                "-hls_flags", "delete_segments+omit_endlist+append_list",
                "-hls_segment_type", "fmp4",
                str(STREAM_DIR / "stream.m3u8")]
        app.logger.info(f"[stream] FFmpeg cmd: {' '.join(cmd)}")
        return cmd, "hls"

    # ── Fallback ──
    app.logger.warning(f"[stream] Unexpected channel combo: {active}")
    return None


def run_ffmpeg(cmd, tag="ffmpeg", stdout_target=subprocess.DEVNULL):
    app.logger.info(f"[{tag}] {' '.join(cmd)}")
    # Log stderr to a file for debugging
    logdir = Path(os.environ.get("STREAM_DIR", "/hls")) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = open(str(logdir / f"{tag}.log"), "a")
    logfile.write(f"\n--- [{tag}] {datetime.now().isoformat()} ---\n")
    logfile.write(f"{' '.join(cmd)}\n")
    logfile.flush()
    return subprocess.Popen(cmd, stdout=stdout_target, stderr=logfile,
                            preexec_fn=lambda: (
                                signal.signal(signal.SIGTERM, lambda s, f: None),
                                signal.signal(signal.SIGPIPE, signal.SIG_IGN),
                            ))


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


# ─── Watchdog — auto-restart ffmpeg on crash ───
watchdog_active = False
restart_count = 0
last_restart_time = 0


def watchdog_loop():
    global ffmpeg_proc, watchdog_active, restart_count, last_restart_time
    while watchdog_active:
        time.sleep(WATCHDOG_INTERVAL)
        if ffmpeg_proc is None:
            continue
        rc = ffmpeg_proc.poll()
        if rc is not None:
            now = time.time()
            app.logger.warning(f"[watchdog] ffmpeg exited with code {rc}")
            # Cooldown check: if we've restarted too many times, back off
            if restart_count >= WATCHDOG_MAX_RESTARTS and (now - last_restart_time) < WATCHDOG_COOLDOWN:
                app.logger.warning(f"[watchdog] Max restarts ({WATCHDOG_MAX_RESTARTS}) reached, "
                                   f"cooling down {WATCHDOG_COOLDOWN}s")
                continue
            # Auto-restart — good faith attempt (on any non-zero exit)
            if rc != 0:
                app.logger.info(f"[watchdog] Auto-restarting ffmpeg (attempt {restart_count+1}/{WATCHDOG_MAX_RESTARTS})")
                _start_stream(suppress_watchdog=True)
                restart_count += 1
                last_restart_time = now


# ═══════════════════════════════════════════
# ROUTES
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
        info["v4l2_detected"] = stream_config["video_dev"] in r.stdout
        info["devices"] = r.stdout
    except Exception:
        pass
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
    result = _start_stream()
    status_code = 500 if result.get("status") == "error" else 200
    return jsonify(result), status_code


def _start_stream(suppress_watchdog=False):
    global ffmpeg_proc, watchdog_active
    try:
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            return {"status": "already_running"}
        # Check video device exists
        if not Path(stream_config["video_dev"]).exists():
            return {"status": "error", "message": f"Video device {stream_config['video_dev']} not found"}
        result = make_ffmpeg_cmd()
        if result is None:
            return {"status": "error", "message": "No channels enabled"}
        cmd, mode = result
        app.logger.info(f"[stream] Starting: {' '.join(cmd)}")
        # Always use DEVNULL for stdout (no pipe output needed)
        ffmpeg_proc = run_ffmpeg(cmd, "stream", stdout_target=subprocess.DEVNULL)
        time.sleep(1.5)
        if ffmpeg_proc is not None and ffmpeg_proc.poll() is not None:
            app.logger.error(f"[stream] ffmpeg died. exit={ffmpeg_proc.returncode}")
            # Read last few lines of ffmpeg log to get actual error
            log_path = STREAM_DIR / "logs" / "stream.log"
            stderr_hint = ""
            if log_path.exists():
                try:
                    lines = log_path.read_text().splitlines()
                    # Get lines after the last "--- [stream] ---" marker
                    cutoff = max((i for i, l in enumerate(lines) if "--- [stream] " in l), default=0)
                    tail = [l for l in lines[cutoff:] if l.strip()]
                    stderr_hint = "\n".join(tail[-15:])
                except Exception:
                    pass
            msg = f"ffmpeg exit code {ffmpeg_proc.returncode}"
            if stderr_hint:
                msg += f"\n--- ffmpeg stderr ---\n{stderr_hint}"
            return {"status": "error", "message": msg}
        # Launch watchdog thread (unless suppressed, e.g. from watchdog auto-restart)
        if not suppress_watchdog and not watchdog_active:
            watchdog_active = True
            t = threading.Thread(target=watchdog_loop, daemon=True)
            t.start()
            app.logger.info("[watchdog] Started")
        return {"status": "ok"}
    except Exception as e:
        app.logger.exception(f"[stream] Start error: {e}")
        return {"status": "error", "message": str(e)}


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    return jsonify(_stop_stream())


def _stop_stream():
    global ffmpeg_proc, restart_count
    if ffmpeg_proc:
        stop_process(ffmpeg_proc)
        ffmpeg_proc = None
    for f in STREAM_DIR.glob("*"):
        if f.is_file() and f.name != "logs":
            try:
                f.unlink()
            except Exception:
                pass
    # Reset restart count on clean stop
    restart_count = 0
    return {"status": "ok"}


@app.route("/api/stream/restart", methods=["POST"])
def stream_restart():
    with app.app_context():
        _stop_stream()
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
        return jsonify({"status": "ok", "config": stream_config,
                        "presets": list(RES_PRESETS.keys()), "current_preset": cp})
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
    return jsonify({"status": "ok", "running": running, "hls_ready": hls_ready,
        "config": stream_config,
        "channels": {k: {"enabled": v["enabled"], "name": v["name"]} for k, v in channels.items()},
        "current_preset": cp
    })


@app.route("/api/stream/log", methods=["GET"])
def stream_log():
    """Return last N lines of ffmpeg stderr log for debugging."""
    n = int(request.args.get("n", 50))
    log_path = STREAM_DIR / "logs" / "stream.log"
    if not log_path.exists():
        return jsonify({"status": "ok", "lines": [], "error": "no log file"})
    try:
        lines = log_path.read_text().splitlines()
        return jsonify({"status": "ok", "lines": lines[-n:]})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


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
    for k in ("enabled", "rtmp_url", "rtmp_key", "port"):
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
    cmd = ["ffmpeg", "-y",
           "-f", "v4l2",
           "-framerate", str(fps), "-video_size", res,
           "-i", stream_config["video_dev"], *encoder_flags(stream_config["hw_encoder"], "4M", low_latency=True)]
    if rc["mode"] == "segment":
        cmd += ["-f", "segment", "-segment_time", str(rc["segment_seconds"]),
                "-reset_timestamps", "1", "-strftime", "1",
                str(od / f"capture_{now}_%03d.mp4")]
    else:
        cmd += [str(od / f"capture_{now}.mp4")]
    record_proc = run_ffmpeg(cmd, "record")
    time.sleep(0.8)
    if record_proc and record_proc.poll() is not None:
        return jsonify({"status": "error", "message": f"ffmpeg exit {record_proc.returncode}"}), 500
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
    return jsonify({"status": "ok", "running": running,
        "files": [{"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1)}
                  for f in files[:20]],
        "disk_used_mb": total_mb})


@app.route("/api/record/files/<path:filename>")
def record_download(filename):
    fp = RECORD_DIR / filename
    if not fp.exists():
        return jsonify({"status": "error", "message": "File not found"}), 404
    return send_file(str(fp), mimetype="video/mp4")


# ── HLS segments ──
@app.route("/api/debug/hls")
def debug_hls():
    import os
    files = []
    try:
        for f in sorted(os.listdir(str(STREAM_DIR))):
            fp = STREAM_DIR / f
            files.append({"name": f, "size": fp.stat().st_size if fp.is_file() else "dir", "exists": fp.exists()})
    except Exception as e:
        return jsonify({"error": str(e), "stream_dir": str(STREAM_DIR), "stream_dir_exists": STREAM_DIR.exists()})
    return jsonify({"stream_dir": str(STREAM_DIR), "files": files[-20:]})

@app.route("/api/debug/audio")
def debug_audio():
    """Test audio device detection and return info."""
    import subprocess
    result = {
        "env_audio_dev": os.environ.get("AUDIO_DEV", ""),
        "arecord_l": "",
        "proc_cards": "",
        "detected_device": None
    }
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=2)
        result["arecord_l"] = r.stdout or r.stderr
    except Exception as e:
        result["arecord_l"] = f"error: {e}"
    try:
        r = subprocess.run(["cat", "/proc/asound/cards"], capture_output=True, text=True, timeout=2)
        result["proc_cards"] = r.stdout or r.stderr
    except Exception as e:
        result["proc_cards"] = f"error: {e}"
    # Try detection
    dev = detect_audio_device()
    result["detected_device"] = dev
    return jsonify(result)

@app.route("/hls/<path:filename>")
def serve_hls(filename):
    fp = STREAM_DIR / filename
    if not fp.exists():
        return jsonify({"error": "not found", "checked_path": str(fp), "stream_dir": str(STREAM_DIR)}), 404
    ct = "video/mp4"
    if filename.endswith(".m3u8"):
        ct = "application/vnd.apple.mpegurl"
    return send_file(str(fp), mimetype=ct)


@app.route("/recordings/<path:filename>")
def serve_recording(filename):
    fp = RECORD_DIR / filename
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(fp), mimetype="video/mp4")


# ═══════════════════════════════════════════
# Chromecast ADB Remote — with keep-alive
# ═══════════════════════════════════════════
CC_CONNECTED = False
CC_LAST_PING = 0.0
CC_RECONNECTING = False


def adb_cmd(args, timeout=5, reconnect_if_dead=False):
    """ADB wrapper with optional auto-reconnect on dead connection."""
    global CC_CONNECTED, CC_LAST_PING, CC_RECONNECTING

    if not CC_HOST:
        return False, "CC_HOST not configured"

    # If connection is known dead and reconnect is enabled, try once
    if reconnect_if_dead and not CC_CONNECTED:
        CC_RECONNECTING = True
        ok, out = _adb_try_reconnect()
        CC_RECONNECTING = False
        if not ok:
            return False, f"reconnect failed: {out}"

    full_cmd = ["adb", "-s", f"{CC_HOST}:{ADB_PORT}"] + args
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        # On success, mark connected and update last ping
        if r.returncode == 0:
            CC_CONNECTED = True
            CC_LAST_PING = time.time()
        return r.returncode == 0, r.stdout.strip()
    except subprocess.TimeoutExpired:
        CC_CONNECTED = False
        return False, "adb timeout"
    except FileNotFoundError:
        return False, "adb not found"


def _adb_try_reconnect():
    """Try to reconnect to the ADB device. Returns (ok, message)."""
    if not CC_HOST:
        return False, "no host"
    try:
        r = subprocess.run(
            ["adb", "connect", f"{CC_HOST}:{ADB_PORT}"],
            capture_output=True, text=True, timeout=5
        )
        out = r.stdout.strip()
        connected = "connected" in out or "already" in out
        if connected:
            CC_CONNECTED = True
            CC_LAST_PING = time.time()
        return connected, out
    except subprocess.TimeoutExpired:
        return False, "connect timeout"
    except FileNotFoundError:
        return False, "adb not found"


def cc_keepalive():
    """Background thread: ping ADB every 10s, auto-reconnect on disconnect."""
    global CC_CONNECTED, CC_LAST_PING
    app.logger.info("[cc] keep-alive thread started")
    while True:
        time.sleep(10)
        if not CC_HOST:
            continue
        ok, _ = adb_cmd(["get-state"], timeout=3, reconnect_if_dead=True)
        if ok:
            CC_CONNECTED = True
            CC_LAST_PING = time.time()
            app.logger.debug("[cc] keep-alive OK")
        else:
            old = CC_CONNECTED
            CC_CONNECTED = False
            if old:
                app.logger.warning("[cc] keep-alive FAILED — device may be asleep")
            # Try explicit reconnect
            if not CC_RECONNECTING:
                _adb_try_reconnect()


# Start keep-alive thread
_thread = threading.Thread(target=cc_keepalive, daemon=True, name="cc-keepalive")
_thread.start()


@app.route("/api/cc/keepalive", methods=["GET"])
def cc_keepalive_status():
    """Frontend polls this every 5s for connection state."""
    return jsonify({
        "connected": CC_CONNECTED,
        "last_ping": CC_LAST_PING,
        "host": CC_HOST,
        "reconnecting": CC_RECONNECTING,
    })


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
    return jsonify({"status": "error", "message": f"Failed: {out}"}), 502


@app.route("/api/cc/status", methods=["GET"])
def cc_status():
    ok, out = adb_cmd(["get-state"])
    return jsonify({"status": "ok", "connected": ok,
        "host": CC_HOST, "device_state": out if ok else "disconnected"})


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
    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key], reconnect_if_dead=True)
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/vol/<action>", methods=["POST"])
def cc_vol(action):
    KEY_MAP = {"up": "KEYCODE_VOLUME_UP", "down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE"}
    adb_key = KEY_MAP.get(action.lower())
    if not adb_key:
        return jsonify({"status": "error", "message": f"Unknown action: {action}"}), 400
    ok, out = adb_cmd(["shell", "input", "keyevent", adb_key], reconnect_if_dead=True)
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
    ok, out = adb_cmd(["shell", "monkey", "-p", pkg, "1"], reconnect_if_dead=True)
    return jsonify({"status": "ok" if ok else "error", "message": f"Launched {name}" if ok else out})


@app.route("/api/cc/text", methods=["POST"])
def cc_text():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"status": "error", "message": "No text"}), 400
    ok, out = adb_cmd(["shell", "input", "text", text], reconnect_if_dead=True)
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/cc/screenshot", methods=["GET"])
def cc_screenshot():
    screenshot_path = SCR_DIR / "cc_screen.png"
    ok, _ = adb_cmd(["shell", "screencap", "-p", "/sdcard/screen.png"], reconnect_if_dead=True)
    if not ok:
        return jsonify({"status": "error", "message": "screencap failed"}), 502
    ok, _ = adb_cmd(["pull", "/sdcard/screen.png", str(screenshot_path)], reconnect_if_dead=True)
    if not ok:
        return jsonify({"status": "error", "message": "pull failed"}), 502
    if screenshot_path.exists():
        return send_file(str(screenshot_path), mimetype="image/png")
    return jsonify({"status": "error", "message": "screenshot not found"}), 500


# ── Web UI (catch-all) ──
@app.route("/")
def serve_index():
    html = (WEB_UI_DIR / "index.html").read_text()
    # Inject cache-busting version to prevent TV browsers from serving stale JS
    try:
        v = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                    cwd=str(WEB_UI_DIR.parent), text=True, timeout=2).strip()
    except Exception:
        v = datetime.now().strftime("%Y%m%d%H%M")
    html = html.replace('src="app.js"', f'src="app.js?v={v}"')
    html = html.replace('src="hls.min.js"', f'src="hls.min.js?v={v}"')
    html = html.replace('href="style.css"', f'href="style.css?v={v}"')
    return html


@app.route("/<path:filename>")
def serve_ui(filename):
    if not filename:
        return send_from_directory(str(WEB_UI_DIR), "index.html")
    fp = WEB_UI_DIR / filename
    if fp.exists() and fp.is_file():
        ext = fp.suffix.lower()
        if ext in ('.html', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif',
                   '.svg', '.ico', '.webp', '.woff', '.woff2', '.json', '.map', '.txt'):
            return send_from_directory(str(WEB_UI_DIR), filename)
    return send_from_directory(str(WEB_UI_DIR), "index.html")


if __name__ == "__main__":
    app.logger.info(f"TV-STREAM Backend starting on port {PORT}")
    app.logger.info(f"WEB_UI_DIR={WEB_UI_DIR}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
