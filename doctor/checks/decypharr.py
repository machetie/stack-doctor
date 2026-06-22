"""Check: decypharr."""
import os
import sys
import json
import re
import time
import signal
import subprocess
import threading
import logging
import logging.handlers
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ..config import *
from ..clients import *
from ..state import *

# Errnos that indicate a dead/stuck FUSE mount rather than a normal IO error.
_FUSE_ERRNOS = frozenset({
    5,   # EIO (Input/output error)
    6,   # ENXIO (No such device or address / Socket not connected on some kernels)
    107, # ENOTCONN (Transport endpoint is not connected)
})

def _fuse_error(exc):
    """Return True if exc looks like a dead/stuck FUSE mount."""
    if isinstance(exc, OSError):
        if exc.errno in _FUSE_ERRNOS:
            return True
        msg = str(exc).lower()
        if any(x in msg for x in ("socket not connected", "transport endpoint is not connected", "input/output error", "no such device")):
            return True
    return False

def _read_test(path, timeout):
    """Return True if a file under path reads its first bytes within timeout.
    Return False if read failed or FUSE is dead/stuck.
    Return None if the path is empty or we cannot list it for a benign reason."""
    result = {"ok": False, "fuse_dead": False}
    target = {"f": None}
    try:
        for root, _, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".ts")):
                    target["f"] = os.path.join(root, fn); break
            if target["f"]:
                break
    except OSError as e:
        if _fuse_error(e):
            result["fuse_dead"] = True
            return result
        return None  # cannot even list -> unknown
    except Exception:
        return None  # cannot even list -> unknown
    if not target["f"]:
        return None
    def _do():
        try:
            with open(target["f"], "rb") as fh:
                fh.read(65536)
            result["ok"] = True
        except OSError as e:
            result["ok"] = False
            if _fuse_error(e):
                result["fuse_dead"] = True
        except Exception:
            result["ok"] = False
    th = threading.Thread(target=_do, daemon=True); th.start(); th.join(timeout)
    if th.is_alive():
        return False  # hung
    return result["ok"] if not result["fuse_dead"] else result

_decy_last_restart = [0.0]
def _decy_restart(reason=""):
    """Run the decypharr restart hook to recover a hung mount, rate-limited to once / 5 min.
    Shared by the decypharr check and the plexscan check. Returns True if the hook ran."""
    tag = (" (%s)" % reason) if reason else ""
    if DRY_RUN or not DECY_RESTART_CMD:
        log.error("[decypharr] hung but no restart cmd set (or dry-run) -> alert only%s", tag); return False
    if time.time() - _decy_last_restart[0] < 300:
        log.warning("[decypharr] restarted <5m ago, holding off%s", tag); return False
    log.error("[decypharr] running restart hook%s: %s", tag, DECY_RESTART_CMD)
    rc = run_cmd(DECY_RESTART_CMD); _decy_last_restart[0] = time.time()
    log.error("[decypharr] restart hook rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")
    return True

def check_decypharr():
    if DECY_URL:
        c = http_code(DECY_URL, t=10)
        log.info("[decypharr] api %s -> %s", DECY_URL, c if c else "DOWN")
    if not DECY_MOUNT_TEST:
        return
    ok = _read_test(DECY_MOUNT_TEST, DECY_READ_TIMEOUT)
    if ok is None:
        log.warning("[decypharr] mount %s: no test file found / unlistable", DECY_MOUNT_TEST); return
    if ok is True:
        log.info("[decypharr] mount %s read OK", DECY_MOUNT_TEST); return
    if isinstance(ok, dict) and ok.get("fuse_dead"):
        log.error("[decypharr] mount %s DEAD FUSE (socket/transport not connected)", DECY_MOUNT_TEST)
    else:
        log.error("[decypharr] mount %s READ HUNG (FUSE stall)", DECY_MOUNT_TEST)
    _decy_restart("dead_fuse")
