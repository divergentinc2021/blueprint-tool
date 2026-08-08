#!/usr/bin/env bash
# release.sh — cut a GitHub release from the version in bl_info.
#
#   ./release.sh              release the version bl_info already declares
#   ./release.sh 1.3.0        bump bl_info to 1.3.0 first, then release
#
# The tag is derived from bl_info rather than typed twice, so the add-on version and the
# release tag cannot disagree.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ $# -ge 1 ]; then
  NEW="$1"
  echo "$NEW" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must be X.Y.Z" >&2; exit 2; }
  python3 - "$NEW" <<'PY'
import re, sys
new = tuple(int(p) for p in sys.argv[1].split("."))
p = "blueprint_tool/__init__.py"
s = open(p).read()
s2, n = re.subn(r'("version"\s*:\s*)\(\d+,\s*\d+,\s*\d+\)', r'\1(%d, %d, %d)' % new, s, count=1)
if not n:
    raise SystemExit("could not find version in bl_info")
open(p, "w").write(s2)
print("bl_info version -> %d.%d.%d" % new)
PY
fi

VERSION="$(python3 -c "import build_addon; print(build_addon.version())")"
TAG="v$VERSION"
echo "Releasing $TAG"

if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "Release $TAG"
fi

python3 build_addon.py
cp "dist/blueprint_tool-$VERSION.zip" dist/blueprint_tool.zip

git tag -a "$TAG" -m "Blueprint Tool $VERSION" 2>/dev/null || echo "tag $TAG already exists, reusing"
git push origin HEAD
git push origin "$TAG"

if gh release view "$TAG" >/dev/null 2>&1; then
  gh release upload "$TAG" dist/blueprint_tool.zip --clobber
  echo "Updated the asset on existing release $TAG"
else
  gh release create "$TAG" dist/blueprint_tool.zip \
    --title "Blueprint Tool $VERSION" \
    --notes "Download \`blueprint_tool.zip\` below, then in Blender: Edit > Preferences > Add-ons > Install from Disk. Blender 4.5+."
fi
gh release view "$TAG" --json url -q .url
