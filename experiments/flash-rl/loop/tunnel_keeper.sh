#!/bin/bash
# 2026-10-23: KT proxy closes the 18241/18242 forward mid-eval (memory kt-tunnel-remote-close); loop.py only re-opens it when it
# (re)starts servers. Keep the forward alive in between, using the loop's own tunnel.pid so loop.py tunnel() sees it as live.
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
KEY="/home/swlee/projects/kt-key/id_container (3)"
while pgrep -f 'outputs/flash-rl-loop-20261015/loop.py' >/dev/null; do
  pid=$(cat tunnel.pid 2>/dev/null)
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    setsid ssh -q -i "$KEY" -p 10569 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 \
      -o ExitOnForwardFailure=yes -N -L 127.0.0.1:18241:127.0.0.1:18241 -L 127.0.0.1:18242:127.0.0.1:18242 \
      work@proxy1.ainexus.ktcloud.com < /dev/null > /dev/null 2>&1 &
    echo $! > tunnel.pid
    echo "$(date -Is) tunnel was down (pid ${pid:-none}); restarted as $!"
  fi
  sleep 10
done
echo "$(date -Is) loop gone; keeper exits"
