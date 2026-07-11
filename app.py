"""
TV-STREAM Backend — Flask API Server
Serves everything: HLS + Web UI + Recording + API
"""

import os, signal, subprocess, threading, time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=None)  # We serve static ourselves
CORS(app)

# ─── Config ───
STREAM_DIR = Path(os.environ.get("STREAM_DIR", "/hls"))
RECORD_DIR = Path(os.environ.get("RECORD_DIR", "/recordings"))
WEB_UI_DIR = Path("/usr/share/nginx/html")

STREAM_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ───
ffmpeg_proc = None
record_proc = None

stream_config = {
    "resolution": "1920x1080", "fps": 30, "bitrate": "6M",
    "hw_encoder": os.environ.get("HW_ENCODER", "h264_v4l2m2m"),
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
    "enabled": False, "quality": "same", "mode": "segment",
    "segment_seconds": 300, "destination": "local",
    "nas_path": "", "output_dir": str(RECORD_DIR),
}

# ─── ffmpeg helpers ───
def make_ffmpeg_cmd():
    cfg = stream_config
    cmd = ["ffmpeg","-y","-f","v4l2","-input_format","mjpeg",
           "-framerate",str(cfg["fps"]),"-video_size",cfg["resolution"],
           "-i","/dev/video0","-c:v",cfg["hw_encoder"],
           "-b:v",cfg["bitrate"],"-maxrate",cfg["bitrate"],
           "-bufsize",f"{int(cfg['bitrate'].replace('M',''))*2}M",
           "-preset","ultrafast","-g",str(cfg["fps"]),
           "-use_wallclock_as_timestamps","1","-flush_packets","1"]
    active = [ch for ch,info in channels.items() if info["enabled"]]
    n = len(active)
    if n == 0: return None
    idx = 0
    if n > 1:
        splits = ",".join([f"[out_{i}]" for i in range(n)])
        cmd += ["-filter_complex",f"split={n}{splits}"]
    if channels["hls"]["enabled"]:
        if n > 1: cmd += ["-map",f"[out_{idx}]"]; idx += 1
        cmd += ["-f","hls","-hls_time","2","-hls_list_size","10",
                "-hls_flags","delete_segments+omit_endlist",
                "-hls_segment_type","mpegts","-progress","-",
                str(STREAM_DIR/"stream.m3u8")]
    if channels["teams"]["enabled"] and channels["teams"]["rtmp_url"]:
        if n > 1: cmd += ["-map",f"[out_{idx}]"]; idx += 1
        cmd += ["-f","flv",f"{channels['teams']['rtmp_url']}/{channels['teams']['rtmp_key']}"]
    if channels["telegram"]["enabled"] and channels["telegram"]["rtmp_url"]:
        if n > 1: cmd += ["-map",f"[out_{idx}]"]; idx += 1
        cmd += ["-f","flv",channels["telegram"]["rtmp_url"]]
    return cmd

def run_ffmpeg(cmd, tag="ffmpeg"):
    app.logger.info(f"[{tag}] {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=lambda: signal.signal(signal.SIGTERM, lambda s, f: None))

def stop_process(proc):
    if proc is None: return
    try: proc.terminate(); proc.wait(timeout=5)
    except: proc.kill(); proc.wait()

# ─── Routes (ORDER matters: specific before catch-all) ───

# 1. HLS segments
@app.route("/hls/<path:filename>")
def serve_hls(filename):
    fp = STREAM_DIR / filename
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    ct = "video/mp2t"
    if filename.endswith(".m3u8"):
        ct = "application/vnd.apple.mpegurl"
    return send_file(str(fp), mimetype=ct)

# 2. Recorded files
@app.route("/recordings/<path:filename>")
def serve_recording(filename):
    fp = RECORD_DIR / filename
    if not fp.exists(): return jsonify({"error":"not found"}),404
    return send_file(str(fp), mimetype="video/mp4")

# 3. API routes
@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    global ffmpeg_proc
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        return jsonify({"status":"already_running"})
    cmd = make_ffmpeg_cmd()
    if cmd is None: return jsonify({"status":"error","message":"No channels"}),400
    ffmpeg_proc = run_ffmpeg(cmd)
    time.sleep(0.5)
    if ffmpeg_proc.poll() is not None:
        _,err = ffmpeg_proc.communicate()
        return jsonify({"status":"error","message":err[:200]}),500
    return jsonify({"status":"ok"})

@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    global ffmpeg_proc
    if ffmpeg_proc: stop_process(ffmpeg_proc); ffmpeg_proc = None
    return jsonify({"status":"ok"})

@app.route("/api/stream/restart", methods=["POST"])
def stream_restart():
    stream_stop(); time.sleep(0.5); return stream_start()

@app.route("/api/stream/config", methods=["GET","PUT"])
def stream_config_ep():
    global stream_config
    if request.method == "GET":
        cp = "custom"
        for n,p in RES_PRESETS.items():
            if p["resolution"]==stream_config["resolution"] and p["fps"]==stream_config["fps"]:
                cp = n; break
        return jsonify({"status":"ok","config":stream_config,"presets":list(RES_PRESETS.keys()),"current_preset":cp})
    data = request.get_json() or {}
    if "preset" in data and data["preset"] in RES_PRESETS:
        stream_config.update(RES_PRESETS[data["preset"]])
    else:
        for k in ["resolution","fps","bitrate"]:
            if k in data: stream_config[k] = data[k]
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
    return jsonify({"status":"ok","config":stream_config})

@app.route("/api/stream/status", methods=["GET"])
def stream_status():
    r = ffmpeg_proc is not None and ffmpeg_proc.poll() is None
    return jsonify({"status":"ok","running":r,"hls_ready":(STREAM_DIR/"stream.m3u8").exists(),
                    "config":stream_config,"channels":{k:v["enabled"] for k,v in channels.items()},
                    "current_preset":next((n for n,p in RES_PRESETS.items()
                      if p["resolution"]==stream_config["resolution"] and p["fps"]==stream_config["fps"]),"custom")})

@app.route("/api/channel/status", methods=["GET"])
def channel_status():
    return jsonify({"status":"ok","channels":channels})

@app.route("/api/channel/<name>", methods=["GET","PUT"])
def channel_control(name):
    if name not in channels: return jsonify({"status":"error"}),404
    if request.method == "GET": return jsonify({"status":"ok","config":channels[name]})
    data = request.get_json() or {}
    for k in ["enabled","rtmp_url","rtmp_key"]:
        if k in data: channels[name][k] = data[k]
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        threading.Thread(target=lambda: stream_restart(), daemon=True).start()
    return jsonify({"status":"ok","config":channels[name]})

@app.route("/api/record/start", methods=["POST"])
def record_start():
    global record_proc
    if record_proc and record_proc.poll() is None:
        return jsonify({"status":"error","message":"Already recording"}),400
    data = request.get_json() or {}
    rc = {**record_config}
    for k in ["quality","mode","segment_seconds","destination"]:
        if k in data: rc[k] = data[k]
    q = rc["quality"]
    res = stream_config["resolution"] if q=="same" else ("1280x720" if q=="720p" else "1920x1080")
    fps = stream_config["fps"] if q=="same" else 30
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    od = Path(rc["output_dir"]); od.mkdir(parents=True,exist_ok=True)
    cmd = ["ffmpeg","-y","-f","v4l2","-input_format","mjpeg","-framerate",str(fps),"-video_size",res,
           "-i","/dev/video0","-c:v","h264_v4l2m2m","-b:v","4M","-preset","ultrafast",
           "-use_wallclock_as_timestamps","1"]
    if rc["mode"]=="segment":
        cmd += ["-f","segment","-segment_time",str(rc["segment_seconds"]),"-reset_timestamps","1","-strftime","1",
                str(od/f"capture_{now}_%03d.mp4")]
    else:
        cmd += [str(od/f"capture_{now}.mp4")]
    record_proc = run_ffmpeg(cmd,"record")
    time.sleep(0.5)
    if record_proc and record_proc.poll() is not None:
        _,err = record_proc.communicate()
        return jsonify({"status":"error","message":err[:200]}),500
    return jsonify({"status":"ok"})

@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    global record_proc
    if record_proc: stop_process(record_proc); record_proc = None
    return jsonify({"status":"ok"})

@app.route("/api/record/status", methods=["GET"])
def record_status():
    r = record_proc is not None and record_proc.poll() is None
    files = sorted(RECORD_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    return jsonify({"status":"ok","running":r,
        "files":[{"name":f.name,"size_mb":round(f.stat().st_size/1048576,1)} for f in files[:20]],
        "disk_used_mb":sum(f.stat().st_size for f in files)/1048576 if files else 0})

@app.route("/api/record/files/<filename>")
def record_download(filename):
    fp = RECORD_DIR / filename
    if not fp.exists(): return jsonify({"status":"error"}),404
    return send_file(str(fp), mimetype="video/mp4")

@app.route("/api/system/info")
def system_info():
    info = {"v4l2_detected":False}
    try:
        r = subprocess.run(["v4l2-ctl","--list-devices"],capture_output=True,text=True,timeout=3)
        info["v4l2_detected"]="/dev/video0" in r.stdout
    except: pass
    return jsonify({"status":"ok","info":info})

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","service":"tv-stream-backend"})

# 4. Web UI — catch-all (MUST be LAST)
@app.route("/")
@app.route("/<path:filename>")
def serve_ui(filename=""):
    if not filename:
        return send_from_directory(str(WEB_UI_DIR), "index.html")
    # Don't intercept /hls/ or /api/ or /recordings/
    if filename.startswith("hls/") or filename.startswith("api/") or filename.startswith("recordings/"):
        return send_from_directory(str(WEB_UI_DIR), "index.html")
    fp = WEB_UI_DIR / filename
    if not fp.exists():
        return send_from_directory(str(WEB_UI_DIR), "index.html")
    return send_from_directory(str(WEB_UI_DIR), filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
