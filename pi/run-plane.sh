#!/bin/bash
# Launch plane.py in the background, write its pid to a file, leave logs visible.
cd "$(dirname "$0")"
# Kill any previous instance first
if [ -f plane.pid ]; then
    kill "$(cat plane.pid)" 2>/dev/null
    rm -f plane.pid
fi
nohup python3 plane.py > plane.log 2>&1 &
echo $! > plane.pid
sleep 2
echo "pid=$(cat plane.pid)"
if kill -0 "$(cat plane.pid)" 2>/dev/null; then
    echo "STATUS: running"
else
    echo "STATUS: died — log follows"
    cat plane.log
fi
