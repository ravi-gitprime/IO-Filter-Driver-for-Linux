#!/bin/bash
# Build rkcdp userspace as Nuitka onefile binaries into ./dist (run on the build box).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dist
for prog in rkcdpd rkcdp-rebuild; do
    echo "== $prog"
    ionice -c3 nice -n19 python3 -m nuitka --onefile --output-dir=dist --output-filename=$prog \
        --include-module=dmcdp --assume-yes-for-downloads --remove-output "$prog.py"
done
ls -la dist/
