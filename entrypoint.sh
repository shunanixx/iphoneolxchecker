#!/bin/sh
# Runs as root so it can fix ownership of the bind-mounted ./data volume,
# then drops to appuser for the actual process.
#
# On a fresh checkout, `./data` doesn't exist on the host yet — Docker
# creates it on first mount, owned by root, regardless of what the image
# chowned at build time (a bind mount's permissions come from the host
# side, not the image layer). Without this, the very first
# `docker compose up` on a clean clone fails with "unable to open
# database file", because appuser (uid 1000, not root) can't write into
# a root-owned directory.
set -e

chown -R appuser:appuser /app/data

exec setpriv --reuid=appuser --regid=appuser --clear-groups "$@"
