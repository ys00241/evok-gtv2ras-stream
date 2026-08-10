# Low-Latency Streaming Analysis & Improvement Plan

## 來源 Repo 分析總結

### PiStream-Lite (855princekumar/PiStream-Lite)
- **Pipeline**: USB Webcam → FFmpeg H.264 → MediaMTX RTSP Server
- **低延時 Flags**:
  ```
  -fflags nobuffer+discardcorrupt+genpts -flags low_delay -muxdelay 0
  ```
- **Thread Queue**: 2048/4096 (視乎 input source)
- **HLS 選項**: `-hls_time 0.5` 可選
- **特色**: Auto-recovery, hot-plug support, systemd supervision

### rpi-hdmi-ndi-streamer (cmelakmartin/rpi-hdmi-ndi-streamer)
- **Pipeline**: HDMI Capture → v4l2loopback → v4l2ndi (NDI output)
- **延時**: <1s (RPi4B), <2s (RPi3B)
- **FFmpeg Flags**:
  ```bash
  ffmpeg -fflags nobuffer -thread_queue_size $THREAD_QUEUE_SIZE -avoid_negative_ts make_zero \
  -f v4l2 -input_format mjpeg -video_size ${WIDTH}x${HEIGHT} -framerate $FPS \
  -i $DEVICE_IN -pix_fmt yuyv422 -vcodec rawvideo -f v4l2 $DEVICE_OUT
  ```
- **Encoder Config** (ndi-config.env + v4l2ndi):
  - `-preset ultrafast -tune zerolatency`
  - `-bf 0` (no B-frames)
  - `-g 30` (GOP = 30 frames = 1s @ 30fps)

---

## 現有 evok-gtv2ras-stream 問題對照表

| 問題 | 現狀 | 目標值 | 來源 |
|------|------|--------|------|
| HLS segment duration | 1s | 0.5s | PiStream-Lite |
| HLS list size | 5 | 3 | 減少 buffer |
| thread_queue_size (video) | 512 | 2048 | PiStream-Lite |
| thread_queue_size (audio) | 512 | 1024 | 適度增加 |
| `-fflags nobuffer` | 缺失 | 加上 | 兩個 repo 都有 |
| `-flags low_delay` | 缺失 | 加上 | PiStream-Lite |
| `-muxdelay 0` | 缺失 | 加上 | PiStream-Lite |
| encoder preset | veryfast | ultrafast | rpi-hdmi-ndi-streamer |
| encoder tune | 無 | zerolatency | rpi-hdmi-ndi-streamer |
| B-frames (`-bf`) | 默認 (3) | 0 | rpi-hdmi-ndi-streamer |
| GOP (`-g`) | 默認 (250) | 30 (1s @ 30fps) | rpi-hdmi-ndi-streamer |
| watchdog cooldown | 30s | 10s | 更快恢復 |
| `-avoid_negative_ts` | 缺失 | make_zero | rpi-hdmi-ndi-streamer |

---

## 詳細改動建議

### 1. HLS Segment Duration & List Size
**現有代碼** (app.py:142-145, 170-173):
```python
cmd += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
        "-hls_flags", "delete_segments+omit_endlist",
        "-hls_segment_type", "mpegts",
        str(STREAM_DIR / "stream.m3u8")]
```

**建議改動**:
```python
cmd += ["-f", "hls", "-hls_time", "0.5", "-hls_list_size", "3",
        "-hls_flags", "delete_segments+omit_endlist+append_list",
        "-hls_segment_type", "mpegts",
        str(STREAM_DIR / "stream.m3u8")]
```

**原因**: PiStream-Lite 用 0.5s segment。list_size 3 = 1.5s 理論延時 (vs 5s)。`append_list` 避免 playlist rewrite 導致 player 重新加載。

**預期延時影響**: -3.5s (5s → 1.5s)

**風險**: Segment 太短可能增加 I/O 壓力、玩家求段失敗率↑。0.5s 是平衡點，RPi/N100 都撐得住。

---

### 2. FFmpeg Global Input Flags (`-fflags`, `-flags`, `-muxdelay`, `-avoid_negative_ts`)
**現有代碼** (app.py:110-117):
```python
cmd = ["ffmpeg", "-y",
       "-thread_queue_size", "512",
       "-f", "v4l2", "-input_format", "mjpeg",
       "-framerate", str(cfg["fps"]), "-video_size", cfg["resolution"],
       "-i", "/dev/video0"]
```

**建議改動**:
```python
cmd = ["ffmpeg", "-y",
       "-fflags", "nobuffer+discardcorrupt+genpts",
       "-flags", "low_delay",
       "-muxdelay", "0",
       "-avoid_negative_ts", "make_zero",
       "-thread_queue_size", "2048",
       "-f", "v4l2", "-input_format", "mjpeg",
       "-framerate", str(cfg["fps"]), "-video_size", cfg["resolution"],
       "-i", "/dev/video0"]
```

**原因**: 
- `nobuffer` - 減輸入端 buffering (兩個 repo 都有)
- `discardcorrupt` - 捨壞包避免卡頓
- `genpts` - 缺 PTS 時自動生成
- `low_delay` - codec 層面低延時
- `muxdelay 0` - muxer 不延遲
- `avoid_negative_ts make_zero` - timestamp 歸零避免負值 (rpi-hdmi-ndi-streamer)

**預期延時影響**: -0.5~1s

**風險**: `discardcorrupt` 可能丟幀；`low_delay` 某些 encoder 不支援 (libx264 OK)。`muxdelay 0` HLS 情況下效果有限但無害。

---

### 3. Thread Queue Size
**現有代碼** (app.py:111, 116):
```python
"-thread_queue_size", "512",
...
"-thread_queue_size", "512", "-f", "alsa", "-i", audio_device
```

**建議改動**:
```python
"-thread_queue_size", "2048",
...
"-thread_queue_size", "1024", "-f", "alsa", "-i", audio_device
```

**原因**: PiStream-Lite 用 2048/4096。512 在高幀率/高解析度下易觸發 "Thread message queue blocking" 警告。Audio 較輕量，1024 足夠。

**預期延時影響**: 間接 -0.2~0.5s (減少 frame drop/重傳)

**風險**: 記憶體佔用微增 (~2MB per queue)，可忽略。

---

### 4. Encoder Flags (`encoder_flags()` function)
**現有代碼** (app.py:92-103):
```python
def encoder_flags(encoder, bitrate):
    flags = ["-c:v", encoder, "-b:v", bitrate]
    if encoder == "libx264":
        flags += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        flags += ["-pix_fmt", "yuv420p"]
    return flags
```

**建議改動**:
```python
def encoder_flags(encoder, bitrate, low_latency=True):
    flags = ["-c:v", encoder, "-b:v", bitrate]
    if low_latency:
        # Low-latency common flags
        flags += ["-bf", "0", "-g", "30", "-keyint_min", "30"]
    if encoder == "libx264":
        if low_latency:
            flags += ["-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
        else:
            flags += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        # v4l2m2m (bcm2835-codec) - hardware encoder
        flags += ["-pix_fmt", "yuv420p"]
        # Note: hw encoder may not support -preset/-tune/-bf/-g
    return flags
```

**調用點更新** (app.py:139, 148, 153, 168, 444):
```python
# 所有 encoder_flags() 調用加上 low_latency=True
cmd += encoder_flags(cfg["hw_encoder"], cfg["bitrate"], low_latency=True)
```

**原因**: rpi-hdmi-ndi-streamer 用 `ultrafast + zerolatency + bf=0 + g=30`。`-bf 0` 禁用 B-frame 減 1-2 幀延時。`-g 30` (1s @ 30fps) 確保 keyframe 頻密，利於 seek/恢復。

**預期延時影響**: -0.5~1s (encoder 端)

**風險**: 
- `ultrafast` 畫質下降、bitrate 效率低 (文件變大 ~20-30%)
- 硬件 encoder (h264_v4l2m2m) 可能不支援 `-bf/-g/-preset/-tune`，需 try/except 或分支處理
- 建議：hw encoder 走舊邏輯，只加 `-g 30` (如果支援)

---

### 5. Watchdog Cooldown & Restart Logic
**現有代碼** (app.py:14-16, 223-243):
```python
WATCHDOG_INTERVAL = 5
WATCHDOG_MAX_RESTARTS = 3
WATCHDOG_COOLDOWN = 30  # 太長

def watchdog_loop():
    ...
    if restart_count >= WATCHDOG_MAX_RESTARTS and (now - last_restart_time) < WATCHDOG_COOLDOWN:
        ...
```

**建議改動**:
```python
WATCHDOG_INTERVAL = 3       # 更頻繁檢查
WATCHDOG_MAX_RESTARTS = 5   # 容許更多重試
WATCHDOG_COOLDOWN = 10      # 10s 冷卻夠用

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
            # Cooldown check
            if restart_count >= WATCHDOG_MAX_RESTARTS and (now - last_restart_time) < WATCHDOG_COOLDOWN:
                app.logger.warning(f"[watchdog] Max restarts reached, cooling down {WATCHDOG_COOLDOWN}s")
                continue
            # Auto-restart on any non-zero exit (crash)
            if rc != 0:
                app.logger.info(f"[watchdog] Auto-restarting ffmpeg (attempt {restart_count+1}/{WATCHDOG_MAX_RESTARTS})")
                _start_stream(suppress_watchdog=True)
                restart_count += 1
                last_restart_time = now
```

**原因**: 30s 冷卻太久，用戶感知到 "掛了半天"。10s 足夠讓 USB device 重新 enumerate。PiStream-Lite 有 auto-recovery 邏輯，參考其快速重試策略。

**預期延時影響**: 故障恢復時間 -20s

**風險**: 過於激進重試可能導致 CPU 佔用飆升 (crash loop)。加上 `WATCHDOG_MAX_RESTARTS=5` + `COOLDOWN=10s` 作為保護。

---

### 6. Recording Pipeline 同步優化
**現有代碼** (app.py:442-449):
```python
cmd = ["ffmpeg", "-y", "-f", "v4l2", "-input_format", "mjpeg",
       "-framerate", str(fps), "-video_size", res,
       "-i", "/dev/video0", *encoder_flags(stream_config["hw_encoder"], "4M"),
       "-use_wallclock_as_timestamps", "1"]
```

**建議改動**: 同步加上低延時 flags (但 recording 不需 HLS 相關)
```python
cmd = ["ffmpeg", "-y",
       "-fflags", "nobuffer+discardcorrupt+genpts",
       "-flags", "low_delay",
       "-muxdelay", "0",
       "-avoid_negative_ts", "make_zero",
       "-thread_queue_size", "2048",
       "-f", "v4l2", "-input_format", "mjpeg",
       "-framerate", str(fps), "-video_size", res,
       "-i", "/dev/video0", *encoder_flags(stream_config["hw_encoder"], "4M", low_latency=True),
       "-use_wallclock_as_timestamps", "1"]
```

**原因**: 錄影同樣受惠於低延時 encoder 設定，尤其直播切片模式。

---

### 7. docker-compose.yml 環境變數補充
**現有代碼** (docker-compose.yml:21-27):
```yaml
environment:
  - STREAM_DIR=/hls
  - RECORD_DIR=/recordings
  - FLASK_ENV=production
  - HW_ENCODER=libx264
  - PORT=6489
  - AUDIO_DEV=hw:1,0
```

**建議新增**:
```yaml
environment:
  - STREAM_DIR=/hls
  - RECORD_DIR=/recordings
  - FLASK_ENV=production
  - HW_ENCODER=libx264
  - PORT=6489
  - AUDIO_DEV=hw:1,0
  # Low-latency tuning
  - HLS_TIME=0.5
  - HLS_LIST_SIZE=3
  - THREAD_QUEUE_VIDEO=2048
  - THREAD_QUEUE_AUDIO=1024
  - ENCODER_PRESET=ultrafast
  - ENCODER_TUNE=zerolatency
  - GOP_SIZE=30
  - BF_FRAMES=0
```

**原因**: 參數化配置，方便不同平台 (N100 vs RPi) 調優，無需改 code 重建 image。

---

## 總結表格

| # | 改動項目 | 預期延時減少 | 風險等級 | 影響範圍 |
|---|----------|-------------|----------|----------|
| 1 | HLS: 1s→0.5s, list 5→3 | **~3.5s** | 中 (I/O↑) | HLS viewer |
| 2 | Global ffmpeg flags (nobuffer, low_delay, muxdelay, avoid_negative_ts) | **~0.5-1s** | 低 | 所有 output |
| 3 | thread_queue_size 512→2048/1024 | **~0.2-0.5s** | 極低 | Input stability |
| 4 | Encoder: ultrafast + zerolatency + bf=0 + g=30 | **~0.5-1s** | 中 (畫質↓, 體積↑) | HLS/RTMP/Record |
| 5 | Watchdog: cooldown 30s→10s, interval 5s→3s | **故障恢復 -20s** | 低 (crash loop 風險) | 可用性 |
| 6 | Recording pipeline 同步優化 | 次要 | 低 | Recording |
| 7 | docker-compose 參數化 | 配置靈活性 | 無 | 部署 |

**總預期延時改善**: **~5-6s** (從 ~6-7s 降至 ~1-2s 理論值)

---

## 完整 Patched app.py (關鍵片段)

### encoder_flags() 重寫
```python
def encoder_flags(encoder, bitrate, low_latency=True):
    """Return ffmpeg encoder flags compatible with given encoder.
    - libx264: software, supports -preset -tune -bf -g
    - h264_v4l2m2m: RPi hardware, limited flag support
    """
    flags = ["-c:v", encoder, "-b:v", bitrate]
    if low_latency:
        # Common low-latency flags (where supported)
        flags += ["-bf", "0", "-g", "30", "-keyint_min", "30"]
    if encoder == "libx264":
        if low_latency:
            flags += ["-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
        else:
            flags += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        # v4l2m2m (bcm2835-codec) — hardware encoder
        # Note: hw encoder may not support -preset/-tune/-bf/-g
        # Only add -pix_fmt; -g might work depending on kernel version
        flags += ["-pix_fmt", "yuv420p"]
        # Try adding GOP control for hw encoder (best effort)
        if low_latency:
            flags += ["-g", "30"]
    return flags
```

### make_ffmpeg_cmd() - Input Section
```python
def make_ffmpeg_cmd():
    cfg = stream_config
    audio_device = detect_audio_device()
    cmd = ["ffmpeg", "-y",
           "-fflags", "nobuffer+discardcorrupt+genpts",
           "-flags", "low_delay",
           "-muxdelay", "0",
           "-avoid_negative_ts", "make_zero",
           "-thread_queue_size", "2048",
           "-f", "v4l2", "-input_format", "mjpeg",
           "-framerate", str(cfg["fps"]), "-video_size", cfg["resolution"],
           "-i", "/dev/video0"]
    if audio_device:
        cmd += ["-thread_queue_size", "1024", "-f", "alsa", "-i", audio_device]
    cmd += ["-use_wallclock_as_timestamps", "1"]
    ...
```

### make_ffmpeg_cmd() - HLS Output Section (兩處)
```python
# HLS only (line ~138-146)
cmd += encoder_flags(cfg["hw_encoder"], cfg["bitrate"], low_latency=True)
if audio_device:
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
cmd += ["-f", "hls", "-hls_time", "0.5", "-hls_list_size", "3",
        "-hls_flags", "delete_segments+omit_endlist+append_list",
        "-hls_segment_type", "mpegts",
        str(STREAM_DIR / "stream.m3u8")]

# Dual HLS+MJPEG (line ~166-173)
cmd += ["-map", "[v0]"]
if audio_device: cmd += ["-map", "[a0]"]
cmd += encoder_flags(cfg["hw_encoder"], cfg["bitrate"], low_latency=True)
if audio_device: cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
cmd += ["-f", "hls", "-hls_time", "0.5", "-hls_list_size", "3",
        "-hls_flags", "delete_segments+omit_endlist+append_list",
        "-hls_segment_type", "mpegts",
        str(STREAM_DIR / "stream.m3u8")]
```

### RTMP Outputs (Teams/Telegram) 同步
```python
# Teams (line ~147-151)
cmd += encoder_flags(cfg["hw_encoder"], "2M", low_latency=True)
if audio_device: cmd += ["-c:a", "aac", "-b:a", "128k"]
cmd += ["-f", "flv", f"{channels['teams']['rtmp_url']}/{channels['teams']['rtmp_key']}"]

# Telegram (line ~152-156)
cmd += encoder_flags(cfg["hw_encoder"], "2M", low_latency=True)
if audio_device: cmd += ["-c:a", "aac", "-b:a", "128k"]
cmd += ["-f", "flv", channels["telegram"]["rtmp_url"]]
```

### Recording Pipeline (line ~442-449)
```python
cmd = ["ffmpeg", "-y",
       "-fflags", "nobuffer+discardcorrupt+genpts",
       "-flags", "low_delay",
       "-muxdelay", "0",
       "-avoid_negative_ts", "make_zero",
       "-thread_queue_size", "2048",
       "-f", "v4l2", "-input_format", "mjpeg",
       "-framerate", str(fps), "-video_size", res,
       "-i", "/dev/video0", *encoder_flags(stream_config["hw_encoder"], "4M", low_latency=True),
       "-use_wallclock_as_timestamps", "1"]
```

### Watchdog Constants & Loop
```python
# Constants (line 14-16)
WATCHDOG_INTERVAL = 3
WATCHDOG_MAX_RESTARTS = 5
WATCHDOG_COOLDOWN = 10

# watchdog_loop() (line 223-243)
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
            if restart_count >= WATCHDOG_MAX_RESTARTS and (now - last_restart_time) < WATCHDOG_COOLDOWN:
                app.logger.warning(f"[watchdog] Max restarts reached, cooling down {WATCHDOG_COOLDOWN}s")
                continue
            if rc != 0:
                app.logger.info(f"[watchdog] Auto-restarting ffmpeg (attempt {restart_count+1}/{WATCHDOG_MAX_RESTARTS})")
                _start_stream(suppress_watchdog=True)
                restart_count += 1
                last_restart_time = now
```

---

## docker-compose.yml 完整版

```yaml
services:
  backend:
    image: ghcr.io/ys00241/evok-gtv2ras-stream:latest
    pull_policy: always
    ports:
      - "8964:6489"
    devices:
      - /dev/video0:/dev/video0
      - /dev/snd:/dev/snd
    volumes:
      - /tmp/hls:/hls
      - ./recordings:/recordings
    group_add:
      - audio
      - video
    environment:
      - STREAM_DIR=/hls
      - RECORD_DIR=/recordings
      - FLASK_ENV=production
      - HW_ENCODER=libx264
      - PORT=6489
      - AUDIO_DEV=hw:1,0
      # Low-latency tuning (可被 app.py 讀取覆蓋 defaults)
      - HLS_TIME=0.5
      - HLS_LIST_SIZE=3
      - THREAD_QUEUE_VIDEO=2048
      - THREAD_QUEUE_AUDIO=1024
      - ENCODER_PRESET=ultrafast
      - ENCODER_TUNE=zerolatency
      - GOP_SIZE=30
      - BF_FRAMES=0
    command: ["python", "app.py"]
    restart: unless-stopped
    stop_grace_period: 10s

  cc-remote:
    image: ghcr.io/ys00241/evok-gtv2ras-stream:latest
    network_mode: host
    environment:
      - CC_HOST=192.168.0.???
      - ADB_PORT=5555
    command: ["python", "cc_remote.py"]
    restart: unless-stopped
    profiles:
      - donotstart
```

---

## 實施建議 (Single Commit)

1. **修改 `app.py`**:
   - 更新 constants (WATCHDOG_*)
   - 重寫 `encoder_flags()` 加 `low_latency` 參數
   - 更新 `make_ffmpeg_cmd()` 加 global flags, 更新 HLS params, 調用 encoder_flags(low_latency=True)
   - 更新 recording pipeline 同步 flags
   - 更新 watchdog_loop()

2. **修改 `docker-compose.yml`**:
   - 新增環境變數供未來參數化 (app.py 可選讀取 `os.environ.get()` 覆蓋 defaults)

3. **測試重點**:
   - HLS 播放延時 (用 hls.js / video.js / Safari 原生)
   - MJPEG 延時對比
   - RPi4 (h264_v4l2m2m) vs N100 (libx264) 畫質/CPU
   - 熱拔插 USB capture card 恢復時間
   - 錄影檔案完整性

---

## 附註：硬件 Encoder 相容性

| Flag | libx264 | h264_v4l2m2m (RPi) |
|------|---------|-------------------|
| -preset | ✅ | ❌ |
| -tune | ✅ | ❌ |
| -bf | ✅ | ⚠️ (kernel 依賴) |
| -g | ✅ | ⚠️ (kernel 依賴) |
| -pix_fmt yuv420p | ✅ | ✅ |

**策略**: `encoder_flags()` 內按 encoder 分支。hw encoder 只強制 `-pix_fmt yuv420p -g 30` (best effort)，其他 flags 只給 libx264。這樣 N100 吃全優化，RPi 吃安全子集。