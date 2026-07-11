# TV-STREAM — Chromecast → RPi 4B 轉播 + Remote Control System

將 Chromecast with Google TV 經 HDMI Splitter + RPi 4B 轉播到 HLS / Teams / Telegram 三個平台，並提供 Web-based Remote Control UI。

> **Repo:** `ys00241/evok-gtv2ras-stream`
> **Hardware:** MS2130 USB HDMI Capture Card + HDMI Splitter 1-in-2-out + RPi 4B 4GB (CasaOS + Portainer)

---

## 📐 System Architecture

```
Chromecast w/ Google TV
    │ HDMI
    ▼
HDMI Splitter 1-in-2-out
    ├──→ TV (本地 0 latency)
    └──→ MS2130 Capture Card
              │ /dev/video0
              ▼
    ┌──────────────────────────────┐
    │  RPi 4B — CasaOS + Portainer  │
    │                               │
    │  backend (Flask)              │  :5000
    │    ├── Stream Manager         │  ffmpeg subprocess
    │    ├── Channel Control        │  HLS / Teams / Telegram
    │    ├── Recording              │  Local / NAS / GDrive
    │    └── Recorder               │  segment mode
    │                               │
    │  cc-remote (Flask) [optional] │  :5001
    │    └── ADB over WiFi          │  D-pad / Vol / Apps / Screenshot
    │                               │
    │  web (nginx)                  │  :8080
    │    ├── SPA UI                 │  Dashboard / Remote / Player / Record
    │    └── API proxy              │  /api/* → backend:5000 / cc-remote:5001
    └──────────────────────────────┘
```

---

## ✅ Features

| Feature | Status | Notes |
|---|---|---|
| HDMI Capture → HLS | ✅ **Done** | ffmpeg + h264_v4l2m2m HW encode |
| 720p@60 / 1080p@30 切換 | ✅ **Done** | Dashboard dropdown → auto restart |
| 3 Channel On/Off (HLS/Teams/TG) | ✅ **Done** | Dashboard toggle; RTMP config UI |
| Web Remote Control UI (Mobile) | ✅ **Done** | SPA dark theme, touch-friendly |
| Live Preview (Expand mode) | ✅ **Done** | hls.js embed + fullscreen |
| HLS Stream Link for TV Browser | ✅ **Done** | `http://rpi:8080/hls/stream.m3u8` |
| QR Code Stream Link | ✅ **Done** | Player tab generate QR |
| Recording (Local) | ✅ **Done** | Segment mode, quality select |
| Chromecast ADB Remote | ✅ **Done** | Optional service (cc-remote) |
| D-pad / Vol / App Launch / Screenshot | ✅ **Done** | via ADB over WiFi |
| Recording → NAS | 🏗️ **v2** | NFS/CIFS mount |
| Recording → Google Drive | 🏗️ **v2** | rclone integration |
| Teams RTMP Push | 🏗️ **v2** | RTMP-In config |
| Telegram Live Push | 🏗️ **v2** | Pyrogram RTMP URL |

---

## 🔧 Hardware Required

| Item | Spec | Budget |
|---|---|---|
| USB HDMI Capture Card | **MS2130** — USB 3.0, 1080p@60, UVC/UAC | ~$150 HKD |
| HDMI Splitter 1-in-2-out | 4K@60Hz, HDCP 1.4 | ~$100 HKD |
| RPi 4B 4GB | CasaOS installed | ✅ 已有 |
| 散熱 | Check heatsink/fan for 1080p encode | ⚠️ 建議加 |

### Capture Card — Linux Plug & Play

> **MS2130 係 UVC (USB Video Class) + UAC (USB Audio Class) 標準**
> 插 Linux 即 detect，kernel 自動 load `uvcvideo` + `snd-usb-audio`
> 唔需要任何 driver

驗證方法：
```bash
v4l2-ctl --list-devices
# → 見到 /dev/video0 就係得
```

---

## 📁 Project Structure

```
evok-gtv2ras-stream/
├── docker-compose.yml       ← Portainer Stack import (3 services)
├── README.md                ← 呢份
│
├── backend/
│   ├── Dockerfile           ← Python 3.12 + ffmpeg (stream/record)
│   ├── Dockerfile.cc        ← Python 3.12 + adb (cc-remote)
│   ├── requirements.txt     ← flask, flask-cors
│   ├── app.py               ← Stream Manager + Recording API
│   └── cc_remote.py         ← Chromecast ADB Remote (standalone)
│
├── web/
│   ├── nginx.conf           ← API proxy + SPA routing
│   ├── index.html           ← Single-Page UI
│   ├── style.css            ← Dark theme, mobile-first
│   └── app.js               ← JavaScript client
│
└── recordings/              ← Recorded files mount
```

---

## 🐳 Docker Compose

### Services

| Service | Port | Description | Required? |
|---|---|---|---|
| **backend** | :5000 | Stream Manager + Recording | ✅ Yes |
| **web** | :8080 | nginx → UI + API proxy | ✅ Yes |
| **cc-remote** | :5001 | Chromecast ADB Remote | ❌ Optional |

### Deploy with Portainer

```
Portainer → Stacks → +Add stack

Name: tv-stream
Repository URL: https://github.com/ys00241/evok-gtv2ras-stream.git
Compose Path: docker-compose.yml
Advanced → ✅ Enable relative path volumes
→ Deploy the stack
```

### Deploy with docker-compose

```bash
git clone git@github.com:ys00241/evok-gtv2ras-stream.git
cd evok-gtv2ras-stream
docker compose up -d
```

### Before first run

**Chromecast ADB（optional）:** 想用 remote control 先要開:

1. Chromecast TV → Settings → Device Preferences → About → Build → tap 7 times
2. Settings → System → Developer Options → USB Debugging → ON
3. Edit `docker-compose.yml` → set `CC_HOST` to Chromecast IP
4. Redeploy → 第一次 TV 會出現 RSA key prompt → accept

---

## 📡 HLS Stream

```
http://<rpi-ip>:8080/hls/stream.m3u8

Example:
  http://192.168.0.18:8080/hls/stream.m3u8
```

| Device | HLS Support |
|---|---|
| Sony BRAVIA (Chrome-based) | ✅ Direct URL / hls.js |
| LG webOS (new) | ✅ hls.js player page |
| LG webOS (old) | ⚠️ Use `/player` tab with hls.js |
| Phone / PC browser | ✅ hls.js |
| VLC / mpv | ✅ Direct m3u8 URL |

---

## 📱 Web UI — 4 Tabs

| Tab | Route | Description |
|---|---|---|
| **📊 Dashboard** | `/` | Stream status, Quality switch, 3 Channel toggles |
| **🎮 Remote** | `/#control` | Virtual remote (D-pad, Vol, Apps, Screenshot, Live expand) |
| **📺 Player** | `/#player` | HLS player, Copy URL, QR code, Fullscreen |
| **💾 Record** | `/#record` | Start/Stop, Quality, Mode, Segment size, File list |

---

## ⚙️ Resolution Config

| Mode | ffmpeg params | Bitrate | Use Case |
|---|---|---|---|
| **720p@60** | `-video_size 1280x720 -framerate 60 -g 60` | 4 Mbps | 動作/波/快速畫面 |
| **1080p@30** | `-video_size 1920x1080 -framerate 30 -g 30` | 6 Mbps | 畫質優先 |

Switch via Dashboard dropdown → backend restarts ffmpeg (< 2s downtime)

---

## 🎮 Chromecast Remote (cc-remote)

Chromecast with Google TV 行 Android TV，經 ADB over WiFi 控制：

| Category | Controls |
|---|---|
| Navigation | ▲ ▼ ◀ ▶ OK BACK HOME SEARCH |
| Volume | Up / Down / Mute |
| Apps | YouTube / Netflix / Disney+ / Prime Video |
| Text | Search text input |
| Screenshot | Capture current screen (PNG) |

**Network:** cc-remote 用 `network_mode: host` — ADB 需要直接 network access。

---

## 💾 Recording

| Setting | Options |
|---|---|
| **Quality** | Same as live / 720p@30 / 1080p@30 |
| **Mode** | Single file / Segment (every N min) |
| **Segment** | 1 / 5 / 10 / 30 min |
| **Destination** | Local `/recordings/` (NAS/GDrive in v2) |

Recording runs as **separate ffmpeg process** — doesn't affect live streaming.

---

## 🗺️ Roadmap

| Phase | What |
|---|---|
| **v1** | ✅ HLS capture + Remote Control + Local Recording |
| **v2** | Teams RTMP, Telegram Live, NAS/GDrive upload |
