#!/usr/bin/env bash
# Double-click this in Finder to install the Blueprint add-on into Blender.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/"
  echo
  read -r -p "Press Return to close."
  exit 1
fi

"$PY" install_addon.py "$@"
STATUS=$?

echo
read -r -p "Press Return to close."
exit $STATUS
