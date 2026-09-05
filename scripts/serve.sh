#!/usr/bin/env bash
# Local dev server for the static reading-library site.
set -euo pipefail
exec python3 -m http.server 8642 --bind 127.0.0.1 --directory "$(dirname "$0")/.."
