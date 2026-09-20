#!/bin/sh
# Build packaging/icon.icns from a square PNG.
#
#     packaging/make_icon.sh path/to/logo.png
#
# macOS wants every size in one .icns; iconutil builds that from a .iconset
# directory, and sips does the resizing.  Start from at least 1024x1024:
# everything below is downscaled from it, and an upscaled source looks soft at
# the sizes people actually see.
set -eu

src=${1:?usage: make_icon.sh <logo.png>}
here=$(cd "$(dirname "$0")" && pwd)
out="$here/icon.icns"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

set=$work/icon.iconset
mkdir -p "$set"

# Each entry is <px>:<name>.  macOS expects both the plain and @2x variants.
for spec in \
    16:icon_16x16 32:icon_16x16@2x \
    32:icon_32x32 64:icon_32x32@2x \
    128:icon_128x128 256:icon_128x128@2x \
    256:icon_256x256 512:icon_256x256@2x \
    512:icon_512x512 1024:icon_512x512@2x
do
    px=${spec%%:*}
    name=${spec#*:}
    sips -z "$px" "$px" "$src" --out "$set/$name.png" >/dev/null
done

iconutil --convert icns "$set" --output "$out"
echo "wrote $out"
