#!/bin/bash
# Build the atlas-transcribe on-device transcription helper (DEC-023).
#
# Produces a single compiled binary at ./atlas-transcribe with the Speech-usage
# Info.plist embedded in its __TEXT,__info_plist section (so macOS can show the
# one-time Speech-recognition permission prompt for a bare CLI binary).
#
# Re-run any time the .swift source changes. Requires Apple's Swift toolchain
# (/usr/bin/swiftc — ships with the Command Line Tools / Xcode).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

/usr/bin/swiftc -O \
  -framework Foundation -framework Speech -framework AVFoundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$DIR/Info.plist" \
  -o "$DIR/atlas-transcribe" \
  "$DIR/atlas-transcribe.swift"

echo "built: $DIR/atlas-transcribe"
