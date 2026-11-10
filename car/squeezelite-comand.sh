#!/bin/bash
# COMAND as a Music Assistant player. MA is on the car's network, or over Tailscale
# from anywhere else.

# One music source at a time: squeezelite only holds an output while it plays (-C),
# so its stream appearing is music starting. Debian's build has no power script.
(
  was=0
  while sleep 2; do
    now=$(pactl list sink-inputs 2>/dev/null | grep -c 'squeezelite')
    if [ "$now" -gt 0 ] && [ "$was" = 0 ]; then
      curl -s -m3 -XPOST -d '{"ma_music":true}' http://127.0.0.1:8080/cmd >/dev/null
    fi
    was=$now
  done
) &

for server in ${HA_HOSTS:-}; do
  if timeout 3 bash -c "</dev/tcp/$server/3483" 2>/dev/null; then
    exec /usr/bin/squeezelite -n COMAND -m 02:00:00:c0:4d:01 -s "$server" -o pulse -a 200:4::0 -C 5
  fi
done
echo "no Music Assistant reachable"
exit 1
