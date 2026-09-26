#!/usr/bin/env bash
set -eu
# LazyCat bind mounts are created as root. Prepare them before returning to the
# upstream Selenium user; the upstream entrypoint and supervisor remain intact.
for path in \
  /app/har_and_cookies \
  /app/generated_media \
  /home/seluser/.g4f \
  /home/seluser/.config; do
  mkdir -p "$path"
  chown -R 1200:1201 "$path"
done
exec su -s /bin/bash -c /opt/bin/entry_point.sh seluser
