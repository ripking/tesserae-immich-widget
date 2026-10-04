#!/bin/sh
# Copy the widget into a local Tesserae Studio workspace so Studio's
# lint / register / faithful_render tools see the repo's current files.
#   STUDIO_DIR=~/tesserae-studio scripts/sync-to-studio.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
dest="${STUDIO_DIR:-$HOME/tesserae-studio}/examples/picture_immich_smart"
mkdir -p "$dest"
rsync -a --delete --exclude '__pycache__' "$here/picture_immich_smart/" "$dest/"
echo "synced -> $dest"
