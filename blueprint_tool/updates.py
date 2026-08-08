"""
Update checking.

Blender copies an add-on into its own scripts folder at install time, so the installed copy
is a frozen snapshot: publishing a new release does not reach it. This module notices a
newer release and says so; installing is still a download and Install from Disk.

Two rules shape the implementation:

* `bpy.app.online_access` is Blender's "Allow Online Access" policy and defaults to OFF.
  An add-on that reaches the network regardless is misbehaving, so every path here checks
  it first and explains what to turn on rather than failing quietly.
* The fetch runs on a worker thread and touches no Blender data. Writing to bpy from a
  thread is a crash, not an error, so the thread only fills a plain dict and the UI reads
  it on the next redraw.
"""

import json
import threading
import urllib.error
import urllib.request

import bpy

DEFAULT_FEED = "https://api.github.com/repos/divergentinc2021/blueprint-tool/releases/latest"
RELEASES_PAGE = "https://github.com/divergentinc2021/blueprint-tool/releases/latest"
TIMEOUT = 10.0

# read by the panel each redraw; only ever written from the main thread's perspective as
# a whole-value replacement, so a half-written state is never observed
_state = {
    "status": "idle",     # idle | checking | ok | newer | error | offline
    "latest": None,       # version tuple of the newest release
    "url": RELEASES_PAGE,
    "message": "",
}


def state():
    return dict(_state)


def parse_version(text):
    """'v1.3.0', '1.3', 'Blueprint Tool 1.3.0' -> (1, 3, 0). None if nothing parses."""
    if not text:
        return None
    digits, cur = [], ""
    for ch in str(text):
        if ch.isdigit():
            cur += ch
        elif ch == "." and cur:
            digits.append(cur)
            cur = ""
        elif cur:
            digits.append(cur)
            break
    if cur:
        digits.append(cur)
    if not digits:
        return None
    nums = tuple(int(d) for d in digits[:3])
    return nums + (0,) * (3 - len(nums))


def _fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "blueprint-tool",
        "Accept": "application/vnd.github+json, application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _interpret(data):
    """Accept a GitHub release payload or a plain {version, url} feed."""
    if not isinstance(data, dict):
        return None, None
    tag = data.get("tag_name") or data.get("version") or data.get("latest")
    url = data.get("html_url") or data.get("url") or RELEASES_PAGE
    return parse_version(tag), url


def check_async(current, feed_url=None, on_done=None):
    """Start a check. `current` is the running version tuple."""
    if not getattr(bpy.app, "online_access", True):
        _state.update(status="offline", message="Allow Online Access is off "
                                                "(Preferences > System)")
        if on_done:
            on_done()
        return False

    url = feed_url or DEFAULT_FEED
    _state.update(status="checking", message="Checking...")

    def work():
        try:
            latest, page = _interpret(_fetch(url))
            if latest is None:
                _state.update(status="error", message="Feed had no version field")
            elif latest > tuple(current):
                _state.update(status="newer", latest=latest, url=page,
                              message="%d.%d.%d available" % latest)
            else:
                _state.update(status="ok", latest=latest, url=page,
                              message="Up to date")
        except urllib.error.HTTPError as e:
            # a private repository answers 404 to an unauthenticated request, which is
            # indistinguishable from a wrong URL unless it is spelled out
            hint = (" (the repository is private, so this needs a public release feed)"
                    if e.code in (401, 403, 404) else "")
            _state.update(status="error", message="HTTP %s%s" % (e.code, hint))
        except Exception as e:
            _state.update(status="error", message=str(e)[:90])
        if on_done:
            on_done()

    threading.Thread(target=work, daemon=True).start()
    return True
