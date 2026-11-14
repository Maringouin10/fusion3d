#!/bin/bash
# Construit dist/Fusion3D-VR.dmg  (à lancer sur un Mac, depuis la racine du dépôt)
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --upgrade pip
pip install -r bridge/requirements.txt pyinstaller

# icône .icns à partir de packaging/icon.png
ICONSET=build/icon.iconset
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s packaging/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) packaging/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o packaging/icon.icns

rm -rf "dist/Fusion3D VR.app" "dist/Fusion3D VR"
pyinstaller --noconfirm --clean packaging/Fusion3DVR.spec

# signature ad hoc (obligatoire sur Apple Silicon)
codesign --force --deep --sign - "dist/Fusion3D VR.app"

STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "dist/Fusion3D VR.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/LISEZMOI.txt "$STAGE/LISEZMOI.txt"

ARCH=$(uname -m)
DMG="dist/Fusion3D-VR-${ARCH}.dmg"
rm -f "$DMG"
# hdiutil échoue parfois au hasard (« Resource busy ») : on réessaie
for i in 1 2 3 4 5; do
  if hdiutil create -volname "Fusion3D VR" -srcfolder "$STAGE" -ov -format UDZO "$DMG"; then
    break
  fi
  if [ "$i" = 5 ]; then echo "hdiutil a échoué 5 fois" >&2; exit 1; fi
  echo "hdiutil occupé, nouvel essai dans $((i * 5)) s…"
  sleep $((i * 5))
done
echo "OK -> $DMG"
