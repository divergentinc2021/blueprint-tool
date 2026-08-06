#!/usr/bin/env python3
"""
install_addon.py — build the add-on and install it into Blender, on macOS or Windows.

    python3 install_addon.py                 find Blender, build, install, enable, save
    python3 install_addon.py --uninstall     disable and remove it
    python3 install_addon.py --list          just show the Blender installs found
    BLENDER=/path/to/blender python3 install_addon.py

Blender does the installing itself, so the add-on lands in the right scripts folder for
whichever Blender version is targeted rather than somewhere this script guessed. Plain
stdlib on the system Python — nothing to install first.
"""

import argparse
import glob
import os
import platform
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = "blueprint_tool"


# ---------------------------------------------------------------- finding blender
def candidate_paths():
    """Every plausible Blender location for this OS, newest-looking first."""
    sysname = platform.system()
    out = []

    if os.environ.get("BLENDER"):
        out.append(os.environ["BLENDER"])

    if sysname == "Darwin":
        pats = [
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "/Applications/Blender*/Blender.app/Contents/MacOS/Blender",
            os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender"),
            os.path.expanduser("~/Applications/Blender*/Blender.app/Contents/MacOS/Blender"),
        ]
    elif sysname == "Windows":
        # Blender installs into a VERSIONED directory, so a fixed path misses every
        # upgrade. Glob and prefer the highest version.
        progs = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        pats = []
        for p in progs:
            if not p:
                continue
            pats += [os.path.join(p, "Blender Foundation", "Blender*", "blender.exe"),
                     os.path.join(p, "Blender Foundation", "blender.exe"),
                     os.path.join(p, "Blender", "blender.exe")]
        pats.append(os.path.join(progs[1] or "", "Steam", "steamapps", "common",
                                 "Blender", "blender.exe"))
    else:
        pats = ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
                os.path.expanduser("~/.local/bin/blender"),
                "/var/lib/flatpak/exports/bin/org.blender.Blender"]

    for pat in pats:
        if any(c in pat for c in "*?"):
            out += sorted(glob.glob(pat), reverse=True)
        else:
            out.append(pat)

    # anything on PATH, last
    from shutil import which
    for name in ("blender", "Blender"):
        w = which(name)
        if w:
            out.append(w)

    seen, uniq = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if rp not in seen and os.path.isfile(p) and os.access(p, os.X_OK):
            seen.add(rp)
            uniq.append(p)
    return uniq


def blender_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        first = (r.stdout or "").strip().splitlines()
        return first[0].strip() if first else "unknown"
    except Exception as e:
        return "unreadable (%s)" % e


def find_blender():
    paths = candidate_paths()
    if not paths:
        raise SystemExit(
            "Could not find Blender.\n"
            "  Install it from https://www.blender.org/download/, or point this at it:\n"
            "    BLENDER=/path/to/blender python3 install_addon.py")
    return paths[0]


# ---------------------------------------------------------------- blender-side work
INSTALL_SNIPPET = """
import bpy, sys
zip_path = %(zip)r
try:
    bpy.ops.preferences.addon_install(overwrite=True, filepath=zip_path)
    bpy.ops.preferences.addon_enable(module=%(mod)r)
    # without this the add-on is enabled only for this process and is gone on restart
    bpy.ops.wm.save_userpref()
    print("RESULT_OK", bpy.app.version_string)
except Exception as e:
    print("RESULT_FAIL", e)
    sys.exit(1)
"""

# bpy.ops.preferences.addon_remove ends with context.area.tag_redraw(), and context.area
# is None in background mode — it raises after having already deleted the files, leaving
# the preferences pointing at an add-on that is gone. Remove the files directly instead.
UNINSTALL_SNIPPET = """
import bpy, addon_utils, os, shutil, sys
mod = %(mod)r
notes, removed = [], []
try:
    addon_utils.disable(mod, default_set=True)
    notes.append("disabled")
except Exception as e:
    notes.append("disable: %%s" %% e)

bases = set(bpy.utils.script_paths(subdir="addons"))
bases.add(bpy.utils.user_resource("SCRIPTS", path="addons"))
for base in bases:
    d = os.path.join(base, mod)
    if os.path.isdir(d):
        shutil.rmtree(d)
        removed.append(d)
    f = os.path.join(base, mod + ".py")
    if os.path.isfile(f):
        os.remove(f)
        removed.append(f)

# a previous half-failed removal can leave a stale preferences entry behind
if mod in bpy.context.preferences.addons:
    try:
        bpy.context.preferences.addons.remove(bpy.context.preferences.addons[mod])
        notes.append("stale preference entry dropped")
    except Exception as e:
        notes.append("pref: %%s" %% e)

try:
    bpy.ops.wm.save_userpref()
except Exception as e:
    print("RESULT_FAIL", e)
    sys.exit(1)
print("REMOVED", removed)
print("NOTES", notes)
print("RESULT_OK", bpy.app.version_string)
"""

# Check both halves: preferences AND files on disk. Either alone can be stale.
VERIFY_SNIPPET = """
import bpy, addon_utils
mod = %(mod)r
in_prefs = mod in bpy.context.preferences.addons
on_disk = any(m.__name__ == mod for m in addon_utils.modules())
print("VERIFY", "enabled" if (in_prefs and on_disk) else "not-enabled",
      "prefs=%%s disk=%%s" %% (in_prefs, on_disk))
"""


def run_in_blender(exe, source):
    """Run a snippet in a fresh background Blender. Returns (ok, combined output)."""
    fd, path = tempfile.mkstemp(suffix=".py", prefix="blueprint_install_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(source)
        r = subprocess.run([exe, "-b", "--python", path],
                           capture_output=True, text=True, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out
    finally:
        os.unlink(path)


def marker(output, name):
    for line in output.splitlines():
        if line.startswith(name):
            return line.strip()
    return None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="install_addon.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true", help="disable and remove the add-on")
    ap.add_argument("--list", action="store_true", help="list the Blender installs found")
    ap.add_argument("--blender", default=None, help="path to the Blender executable")
    args = ap.parse_args()

    if args.list:
        found = candidate_paths()
        if not found:
            print("No Blender found.")
            return 1
        print("Blender installs found (the first is used):")
        for p in found:
            print("  %-60s %s" % (p, blender_version(p)))
        return 0

    exe = args.blender or os.environ.get("BLENDER") or find_blender()
    if not os.path.isfile(exe):
        raise SystemExit("Not a file: %s" % exe)
    print("Blender:  %s" % exe)
    print("          %s" % blender_version(exe))

    if args.uninstall:
        ok, out = run_in_blender(exe, UNINSTALL_SNIPPET % {"mod": MODULE})
        res = marker(out, "RESULT_")
        if not ok or not res or res.startswith("RESULT_FAIL"):
            print(out.strip()[-2000:])
            raise SystemExit("Uninstall failed.")
        print("Removed %s." % MODULE)
        return 0

    # build first so the zip always matches the working tree
    sys.path.insert(0, HERE)
    import build_addon
    zip_path = os.path.join(HERE, "dist", "blueprint_tool-%s.zip" % build_addon.version())
    print("Building: ", end="")
    build_addon.main()

    ok, out = run_in_blender(exe, INSTALL_SNIPPET % {"zip": zip_path, "mod": MODULE})
    res = marker(out, "RESULT_")
    if not ok or not res or res.startswith("RESULT_FAIL"):
        print(out.strip()[-2000:])
        raise SystemExit("Install failed.")

    # confirm it survived into a NEW Blender process; save_userpref is the step that
    # makes the difference, and a missing one looks identical until the next restart
    ok2, out2 = run_in_blender(exe, VERIFY_SNIPPET % {"mod": MODULE})
    verdict = marker(out2, "VERIFY") or "VERIFY unknown"
    if "enabled" not in verdict or "not-enabled" in verdict:
        print(out2.strip()[-2000:])
        raise SystemExit("Installed, but not enabled in a fresh session (%s)." % verdict)

    print("\nInstalled and enabled — verified in a fresh Blender session.")
    print("Open Blender, press N in the 3D viewport, and pick the Blueprint tab.")
    print("Re-run this after pulling changes; Blender keeps its own copy of the add-on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
