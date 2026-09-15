#!/bin/bash
# Build rkcdp userspace as Nuitka onefile binaries into ./dist, then the single
# installer rkcdp-setup with every asset embedded. Run on the build box after
# `make` in the repo root (needs ../dm-cdp.ko).
set -euo pipefail
cd "$(dirname "$0")"
KO=${KO:-../dm-cdp.ko}
[ -f "$KO" ] || { echo "dm-cdp.ko not found at $KO (run make in the repo root)"; exit 1; }
mkdir -p dist
for prog in rkcdpd rkcdp-rebuild rkcdp-seed; do
    echo "== $prog"
    ionice -c3 nice -n19 python3 -m nuitka --onefile --output-dir=dist --output-filename=$prog \
        --include-module=dmcdp --assume-yes-for-downloads --remove-output "$prog.py"
done
echo "== assets"
rm -rf assets && mkdir -p assets
cp "$KO" assets/dm-cdp.ko
cp dist/rkcdpd dist/rkcdp-rebuild dist/rkcdp-seed assets/
cp systemd/rkcdpd.service assets/
cp initramfs/hook initramfs/local-top udev/99-rkcdp.rules assets/
echo "== rkcdp-setup"
ionice -c3 nice -n19 python3 -m nuitka --onefile --output-dir=dist --output-filename=rkcdp-setup \
    --include-data-dir=assets=assets --assume-yes-for-downloads --remove-output rkcdp-setup.py
ls -la dist/
