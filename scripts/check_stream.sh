#!/bin/bash
# TV-STREAM Debug Helper
# Run on RPi to check stream status

echo "=== Container Status ==="
docker ps --filter "name=evok-gtv2ras" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "=== HLS Directory ==="
CONTAINER=$(docker ps --filter "name=evok-gtv2ras" -q | head -1)
if [ -n "$CONTAINER" ]; then
    docker exec $CONTAINER ls -la /hls/ 2>/dev/null | head -20
    echo ""
    echo "=== Latest Segment Info ==="
    docker exec $CONTAINER bash -c "ls -t /hls/*.ts 2>/dev/null | head -1 | xargs ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pixel_format,width,height -of default=noprint_wrappers=1" 2>/dev/null
    echo ""
    echo "=== Stream Log (last 10 lines) ==="
    docker exec $CONTAINER tail -10 /hls/logs/stream.log 2>/dev/null
else
    echo "Container not found!"
fi
