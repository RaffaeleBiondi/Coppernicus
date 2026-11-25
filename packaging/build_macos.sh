#!/usr/bin/env bash
# Build the packaged macOS app and its disk image (Apple silicon):
#
#   packaging/build_macos.sh
#
# Output in dist/ (git-ignored): Coppernicus.app and Coppernicus-<version>-macos-arm64.dmg,
# plus Coppernicus-<version>-macos-arm64-contents.txt (every file and module of the bundle).
#
# Steps: a build environment of its own (build/venv-macos: uv.lock's packages, without the
# dev tools and without triangle, on a uv-managed Python so that the app does not depend on
# a Homebrew or system Python); PyInstaller (packaging/requirements-build.txt) runs
# packaging/coppernicus.spec; packaging/check_bundle.py proves triangle is not in the bundle;
# hdiutil makes the .dmg. The app is not signed with a Developer ID (ad-hoc signature only):
# see docs/install.md for the first start.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ "$(uname -s)" = Darwin ] || { echo "build_macos.sh runs on macOS" >&2; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "build_macos.sh builds for Apple silicon (arm64)" >&2; exit 1; }

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/coppernicus/__init__.py)"
NAME="Coppernicus-${VERSION}-macos-arm64"
export UV_PROJECT_ENVIRONMENT="$ROOT/build/venv-macos"
export UV_PYTHON_PREFERENCE=only-managed
PY="$UV_PROJECT_ENVIRONMENT/bin/python"

echo "== build environment ($UV_PROJECT_ENVIRONMENT)"
uv sync --frozen --no-dev --no-install-package triangle --python 3.12
uv pip install --python "$PY" -r packaging/requirements-build.txt
if "$PY" -c "import triangle" 2>/dev/null; then
  echo "error: triangle is installed in the build environment" >&2
  exit 1
fi

echo "== PyInstaller"
rm -rf dist/Coppernicus dist/Coppernicus.app
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
  packaging/coppernicus.spec
rm -rf dist/Coppernicus                      # the folder BUNDLE made the .app from

echo "== bundle check"
"$PY" packaging/check_bundle.py dist/Coppernicus.app --list "dist/${NAME}-contents.txt"
codesign --verify --deep --strict dist/Coppernicus.app
echo "signature (ad-hoc) verified"

echo "== disk image"
STAGE="build/dmg/Coppernicus"
rm -rf "build/dmg"
mkdir -p "$STAGE/Examples"
ditto dist/Coppernicus.app "$STAGE/Coppernicus.app"
ln -s /Applications "$STAGE/Applications"
for d in examples/*/; do
  ditto "$d" "$STAGE/Examples/$(basename "$d")"
done
cp LICENSE "$STAGE/LICENSE.txt"
cp build/packaging/THIRD_PARTY_NOTICES.txt "$STAGE/THIRD_PARTY_NOTICES.txt"
ditto packaging/licenses "$STAGE/licenses"         # the notices refer to licenses/
cp docs/install.md "$STAGE/INSTALL.md"
rm -f "dist/${NAME}.dmg"
hdiutil create -volname "Coppernicus ${VERSION}" -srcfolder "$STAGE" -fs HFS+ -format UDZO \
  -imagekey zlib-level=9 -ov "dist/${NAME}.dmg" >/dev/null
hdiutil verify "dist/${NAME}.dmg" >/dev/null
echo "Built dist/${NAME}.dmg ($(du -h "dist/${NAME}.dmg" | cut -f1)), app $(du -sh dist/Coppernicus.app | cut -f1)"
