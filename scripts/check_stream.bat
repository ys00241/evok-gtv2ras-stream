@echo off
:: TV-STREAM Quick Check
:: Run on RPi via SSH or directly

echo === Container List ===
docker ps --filter "name=evok-gtv2ras"

echo.
echo === HLS Status ===
for /f "tokens=*" %%c in ('docker ps --filter "name=evok-gtv2ras" -q') do (
    docker exec %%c ls -la /hls/ 2>nul | findstr /i "ts m3u8"
)

echo.
echo === Pixel Format Check ===
for /f "tokens=*" %%c in ('docker ps --filter "name=evok-gtv2ras" -q') do (
    docker exec %%c bash -c "ls -t /hls/*.ts 2>/dev/null | head -1 | xargs ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pixel_format -of default=noprint_wrappers=1"
)
