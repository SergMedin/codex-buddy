#!/usr/bin/env python3
"""Start the Codex Usage Stick BLE bridge once per user session."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPT = PLUGIN_ROOT / "scripts" / "codex_usage_ble_bridge.py"
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
STATE_DIR = DEFAULT_CODEX_HOME / "codex-usage-bridge"
CONFIG_PATH = STATE_DIR / "config.json"
PID_PATH = STATE_DIR / "bridge.pid"
CHILD_PID_PATH = STATE_DIR / "bridge.child.pid"
HEARTBEAT_PATH = STATE_DIR / "bridge.heartbeat"
LOG_PATH = STATE_DIR / "bridge.log"
HOOK_LOG_PATH = STATE_DIR / "hook.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "codex_home": str(DEFAULT_CODEX_HOME),
    "name": "Codex-",
    "address": None,
    "interval": 5.0,
    "scan_timeout": 8.0,
    "restart_delay": 5.0,
    "heartbeat_timeout": 90.0,
    "ble_write_timeout": 8.0,
    "update_timeout": 20.0,
    "verbose": True,
    "no_approval_proxy": True,
}

SHUTDOWN = False


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)


def write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def load_config() -> dict[str, Any]:
    ensure_state_dir()
    if not CONFIG_PATH.exists():
        write_private_text(CONFIG_PATH, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return dict(DEFAULT_CONFIG)
    with contextlib.suppress(OSError):
        CONFIG_PATH.chmod(0o600)
    try:
        loaded = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        loaded = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        cfg.update(loaded)
    return cfg


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if process_alive(pid):
        return pid
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return None


def bridge_command(cfg: dict[str, Any]) -> list[str]:
    cmd = [sys.executable, str(BRIDGE_SCRIPT)]
    codex_home = cfg.get("codex_home")
    if codex_home:
        cmd.extend(["--codex-home", str(codex_home)])
    name = cfg.get("name")
    if name:
        cmd.extend(["--name", str(name)])
    address = cfg.get("address")
    if address:
        cmd.extend(["--address", str(address)])
    if cfg.get("interval") is not None:
        cmd.extend(["--interval", str(cfg["interval"])])
    if cfg.get("scan_timeout") is not None:
        cmd.extend(["--scan-timeout", str(cfg["scan_timeout"])])
    if cfg.get("update_timeout") is not None:
        cmd.extend(["--update-timeout", str(cfg["update_timeout"])])
    if cfg.get("ble_write_timeout") is not None:
        cmd.extend(["--ble-write-timeout", str(cfg["ble_write_timeout"])])
    cmd.extend(["--heartbeat-path", str(HEARTBEAT_PATH)])
    if cfg.get("verbose", True):
        cmd.append("--verbose")
    if cfg.get("no_approval_proxy", True):
        cmd.append("--no-approval-proxy")
    return cmd


def bridge_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["CODEX_HOME"] = str(Path(str(cfg.get("codex_home") or DEFAULT_CODEX_HOME)).expanduser())
    return env


def supervisor_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--supervise"]


def request_shutdown(_signum: int, _frame: object) -> None:
    global SHUTDOWN
    SHUTDOWN = True


def supervise_bridge() -> int:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    while not SHUTDOWN:
        cfg = load_config()
        with contextlib.suppress(FileNotFoundError):
            HEARTBEAT_PATH.unlink()
        proc = subprocess.Popen(bridge_command(cfg), cwd=str(PLUGIN_ROOT), env=bridge_env(cfg))
        write_private_text(CHILD_PID_PATH, f"{proc.pid}\n")
        started_at = time.time()
        heartbeat_timeout = float(cfg.get("heartbeat_timeout", 90.0) or 90.0)
        while proc.poll() is None:
            if SHUTDOWN:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if heartbeat_timeout > 0:
                try:
                    heartbeat_age = time.time() - HEARTBEAT_PATH.stat().st_mtime
                    stale = heartbeat_age > heartbeat_timeout
                except FileNotFoundError:
                    heartbeat_age = time.time() - started_at
                    stale = heartbeat_age > heartbeat_timeout
                if stale:
                    print(
                        f"[supervisor] bridge heartbeat stale for {heartbeat_age:.0f}s; restarting pid {proc.pid}",
                        flush=True,
                    )
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
            time.sleep(1)
        with contextlib.suppress(FileNotFoundError):
            CHILD_PID_PATH.unlink()
        if not SHUTDOWN:
            delay = float(cfg.get("restart_delay", 5.0) or 5.0)
            time.sleep(max(1.0, delay))
    return 0


def start_bridge(foreground: bool = False) -> int:
    cfg = load_config()
    if not BRIDGE_SCRIPT.exists():
        return 2

    if foreground:
        return subprocess.call(bridge_command(cfg), cwd=str(PLUGIN_ROOT), env=bridge_env(cfg))

    pid = running_pid()
    if pid is not None:
        return 0

    ensure_state_dir()
    with LOG_PATH.open("ab") as log:
        LOG_PATH.chmod(0o600)
        proc = subprocess.Popen(
            supervisor_command(),
            cwd=str(PLUGIN_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=bridge_env(cfg),
        )
    write_private_text(PID_PATH, f"{proc.pid}\n")
    return 0


def child_pid() -> int | None:
    try:
        pid = int(CHILD_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if process_alive(pid) else None


def stop_bridge() -> int:
    pid = running_pid()
    child = child_pid()
    for target in (pid, child):
        if target is None:
            continue
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    try:
        CHILD_PID_PATH.unlink()
    except OSError:
        pass
    return 0


def status() -> int:
    cfg = load_config()
    pid = running_pid()
    child = child_pid()
    try:
        heartbeat_age = round(time.time() - HEARTBEAT_PATH.stat().st_mtime, 1)
    except FileNotFoundError:
        heartbeat_age = None
    state = "running" if pid is not None else "stopped"
    print(json.dumps({
        "state": state,
        "pid": pid,
        "child_pid": child,
        "heartbeat": str(HEARTBEAT_PATH),
        "heartbeat_age_sec": heartbeat_age,
        "config": str(CONFIG_PATH),
        "log": str(LOG_PATH),
        "hook_log": str(HOOK_LOG_PATH),
        "command": supervisor_command(),
        "bridge_command": bridge_command(cfg),
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Start/stop the Codex Usage Stick BLE bridge.")
    parser.add_argument("--foreground", action="store_true", help="Run the bridge in the foreground")
    parser.add_argument("--status", action="store_true", help="Print bridge status")
    parser.add_argument("--stop", action="store_true", help="Stop the bridge")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.supervise:
        return supervise_bridge()
    if args.status:
        return status()
    if args.stop:
        return stop_bridge()
    return start_bridge(foreground=args.foreground)


if __name__ == "__main__":
    raise SystemExit(main())
