#!/bin/sh
# Update the BUILD_VERSION constant in index.html with the current UTC
# timestamp. Run this before `git commit` so the deployed page logs an
# accurate version on first user interaction.
#
# Usage:
#   ./bump_build_version.sh && git add index.html && git commit -m "..."
#
# Or alias it: alias gc='./bump_build_version.sh && git add index.html && git commit'

set -e

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Portable in-place sed (BSD/macOS uses '' arg, GNU doesn't). Try both.
if sed --version >/dev/null 2>&1; then
  sed -i "s|const BUILD_VERSION = '[^']*';|const BUILD_VERSION = '${TS}';|" index.html
else
  sed -i '' "s|const BUILD_VERSION = '[^']*';|const BUILD_VERSION = '${TS}';|" index.html
fi

echo "BUILD_VERSION → ${TS}"
