#!/usr/bin/env python3
"""
Send local Codex usage to the StickS3 Codex usage firmware over BLE.

The firmware exposes a Nordic UART Service-compatible BLE endpoint. This
script reads the latest Codex token_count event from ~/.codex, builds the
small JSON packet the firmware expects, then writes it to the NUS RX
characteristic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - user-facing dependency path
    BleakClient = None
    BleakScanner = None


NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
DEFAULT_CODEX_APP_CLI = Path("/Applications/Codex.app/Contents/Resources/codex")
STATE_DIR = DEFAULT_CODEX_HOME / "codex-usage-bridge"
DEFAULT_HOOK_APPROVAL_SOCK = STATE_DIR / "approval.sock"
SNAPSHOT_CACHE_PATH = STATE_DIR / "last_usage_snapshot.json"
PRIMARY_WINDOW_MINUTES = 5 * 60
SECONDARY_WINDOW_MINUTES = 7 * 24 * 60
APP_SERVER_USAGE_SOURCE = Path("account-rateLimits-read")
QUOTA_TTL_SECONDS = 900

INTERESTING_LINE_MARKERS = (
    "token_count",
    "task_started",
    "task_complete",
    "approval",
    "permission",
    "confirm",
    "rate_limit",
    "rate limit",
    "error",
    "failed",
    "exception",
    "traceback",
    "timed out",
)

ATTENTION_EVENT_TYPES = {
    "approval_request",
    "approval_requested",
    "apply_patch_approval_request",
    "permission_request",
    "permission_requested",
    "user_approval_request",
    "tool_approval_request",
}

DIZZY_EVENT_TYPES = {
    "error",
    "fatal_error",
    "task_failed",
    "rate_limit",
    "rate_limit_reached",
}


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def write_private_text(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def touch_heartbeat(args: argparse.Namespace, status: str) -> None:
    path = getattr(args, "heartbeat_path", None)
    if not path:
        return
    payload = {
        "time": time.time(),
        "status": status,
        "pid": os.getpid(),
    }
    try:
        write_private_text(path, json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        pass


def newer_ts(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def is_recent(ts: float | None, window: float, now: float | None = None) -> bool:
    if ts is None:
        return False
    now = time.time() if now is None else now
    return 0 <= now - ts <= window


class ActivityTracker:
    def __init__(self) -> None:
        self.last_tokens: int | None = None
        self.last_event_ts: float | None = None
        self.last_growth_at: float | None = None

    def state_for(self, snapshot: "UsageSnapshot", busy_window: float) -> str:
        now = time.time()

        if self.last_tokens is None:
            self.last_tokens = snapshot.tokens
            self.last_event_ts = snapshot.event_ts
            if snapshot.event_ts and now - snapshot.event_ts <= busy_window:
                self.last_growth_at = now
                return "busy"
            return "idle"

        if snapshot.tokens > self.last_tokens:
            self.last_tokens = snapshot.tokens
            self.last_event_ts = snapshot.event_ts
            self.last_growth_at = now
            return "busy"

        if snapshot.tokens < self.last_tokens:
            self.last_tokens = snapshot.tokens
            self.last_event_ts = snapshot.event_ts
            self.last_growth_at = now
            return "busy"

        if snapshot.event_ts and snapshot.event_ts != self.last_event_ts:
            self.last_event_ts = snapshot.event_ts
            if now - snapshot.event_ts <= busy_window:
                self.last_growth_at = now
                return "busy"

        if self.last_growth_at and now - self.last_growth_at <= busy_window:
            return "busy"
        return "idle"


@dataclass
class UsageSnapshot:
    tokens: int
    primary: int
    secondary: int
    primary_resets_at: int
    secondary_resets_at: int
    source: Path
    event_ts: float | None
    limit_id: str | None
    limit_name: str | None
    task_started_at: float | None = None
    task_complete_at: float | None = None
    attention_at: float | None = None
    dizzy_at: float | None = None
    last_activity_at: float | None = None
    # Observation time survives retries/restarts; live is true only for a new
    # successful API read, never for replayed cache or rollout data.
    quota_observed_at: float | None = None
    quota_live: bool = False
    quota_log_valid: bool = False
    quota_account: str | None = None
    # Distinguish a confirmed logout/unknown identity from a source that has
    # never provided account information. Unscoped logs are unsafe afterwards.
    account_checked: bool = False
    auth_scope: str | None = None
    gift_reset_expires_at: int | None = None
    gift_observed_at: float | None = None

    def quota_status(self, now: float) -> str:
        if (self.quota_observed_at is None
                or not 0 <= now - self.quota_observed_at < QUOTA_TTL_SECONDS
                or now >= self.secondary_resets_at):
            return "unavailable"
        return "fresh" if self.quota_live else "cached"

    def packet(self, state: str) -> dict[str, Any]:
        now = time.time()
        packet = {
            "state": state,
            "tokens": self.tokens,
            "now": int(now),
            "quota_status": self.quota_status(now),
        }
        if self.quota_observed_at is not None:
            packet["quota_observed_at"] = int(self.quota_observed_at)
            packet["quota_valid_until"] = min(
                int(self.quota_observed_at + QUOTA_TTL_SECONDS), self.secondary_resets_at)
        if packet["quota_status"] != "unavailable":
            if window_is_available(self.primary_resets_at, now):
                packet["primary"] = self.primary
                packet["primary_resets_at"] = self.primary_resets_at
            packet["secondary"] = self.secondary
            packet["secondary_resets_at"] = self.secondary_resets_at
        if self.gift_observed_at is not None:
            packet["gift_observed_at"] = int(self.gift_observed_at)
            # A cached credit that has just expired does not prove that no
            # other credits remain. Await the next complete API snapshot.
            if (self.gift_reset_expires_at is not None
                    and (self.gift_reset_expires_at == 0 or self.gift_reset_expires_at > now)):
                packet["gift_reset_expires_at"] = self.gift_reset_expires_at
        return packet


class QuotaForecast:
    """Bounded quota observations [time, used %, deadline, cycle].

    A deadline is API metadata, not a cycle identifier: an unused window can
    slide until first use. Only same-cycle intervals measure consumption.
    """

    HISTORY_VERSION = 2
    HORIZONS = {"secondary_forecast_48h": 2 * 86400, "secondary_forecast_14d": 14 * 86400}
    SAMPLE_SECONDS = 300
    MIN_HISTORY_SECONDS = 3600
    TTL_SECONDS = QUOTA_TTL_SECONDS
    MAX_INTERPOLATION_GAP = 6 * 3600
    MAX_POINTS = 4100
    RESET_TOLERANCE_SECONDS = 5  # API/log reset timestamps can differ by rounding.

    def __init__(self, path: Path):
        self.path = path
        self.key: list[Any] | None = None
        self.points: list[list[float | int]] = []
        self.status = "unavailable"
        self._latest: list[float | int] | None = None
        self._needs_migration = False
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if self.path.stat().st_size > 1024 * 1024:
                return
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or data.get("version") not in (1, self.HISTORY_VERSION):
                return
            version = data["version"]
            points = data.get("points")
            if not isinstance(points, list) or len(points) > self.MAX_POINTS:
                return
            previous_point = None
            for point in points:
                if not self._valid_point(point, version, previous_point):
                    return
                previous_point = point
            latest = data.get("latest", previous_point)
            if version == self.HISTORY_VERSION and points:
                if not self._valid_point(latest, version):
                    return
                if latest != previous_point and not self._valid_point(latest, version, previous_point):
                    return
            elif version == self.HISTORY_VERSION and latest is not None:
                return
            self.key = data.get("key")
            if version == 1:
                # Reclassify old deadline-based cycles and restore the sampling
                # cadence. Migration does not write until a live observation.
                for point in points:
                    self._record(point[0], point[1], point[2])
                if self._latest and self.points[-1] != self._latest:
                    self._append(self._latest)
                self._needs_migration = True
            else:
                self.points = points
                self._latest = latest
        except (OSError, ValueError, TypeError, OverflowError):
            pass

    def _valid_point(self, point: Any, version: int,
                     previous: list[float | int] | None = None) -> bool:
        if not isinstance(point, list) or len(point) != (3 if version == 1 else 4):
            return False
        if not all(type(v) in (int, float) and math.isfinite(v) for v in point):
            return False
        t, p, r = point[:3]
        if not 0 < t < r <= t + 7 * 86400 + self.RESET_TOLERANCE_SECONDS or not 0 <= p <= 100:
            return False
        if version == self.HISTORY_VERSION and (type(point[3]) is not int or point[3] < 0):
            return False
        if previous is None:
            return True
        same_cycle = r == previous[2] if version == 1 else point[3] == previous[3]
        return (t > previous[0] and (not same_cycle or p >= previous[1])
                and (version == 1 or point[3] >= previous[3]))

    def seed(self, current: UsageSnapshot, logs: list[UsageSnapshot],
             key: list[Any], earliest: float = 0) -> None:
        """One-time bootstrap from already-read logs of this live-confirmed cycle."""
        if self.points and self.key == key:
            return
        t, r = current.quota_observed_at, current.secondary_resets_at
        if t is None:
            return
        candidates = [s for s in logs if s.quota_log_valid and s.limit_id == current.limit_id
                      and s.event_ts is not None and max(earliest, r - 7 * 86400) <= s.event_ts < t
                      and abs(s.secondary_resets_at - r) <= self.RESET_TOLERANCE_SECONDS]
        if not candidates or any(s.secondary > current.secondary for s in candidates):
            return
        points = []
        for s in sorted(candidates, key=lambda s: (s.event_ts, -s.secondary)):
            if points and (s.event_ts - points[-1][0] < self.SAMPLE_SECONDS or s.secondary < points[-1][1]):
                continue
            points.append([s.event_ts, s.secondary, r])
        self.key, self.points, self._latest = key, [], None
        for t, p, r in points:
            self._record(t, p, r)
        self.save()

    def save(self) -> None:
        ensure_private_dir(self.path.parent)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as f:
                temp_path = Path(f.name)
                json.dump({"version": self.HISTORY_VERSION, "key": self.key,
                           "points": self.points, "latest": self._latest}, f,
                          separators=(",", ":"), allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
            self._needs_migration = False
            self._dirty = False
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    def _classify(self, t: float, p: float, r: int) -> tuple[bool, bool]:
        """Return (cycle boundary, confirmed reset), independently of sampling."""
        if self._latest is None:
            return False, False
        _, last_p, last_r, _ = self._latest
        elapsed = t >= last_r
        moved = abs(r - last_r) > self.RESET_TOLERANCE_SECONDS
        # A changed deadline after positive usage could hide a reset even if
        # the new percentage has caught up. Exclude that uncertain interval.
        # An unused window may slide or anchor on its first use without reset.
        return elapsed or (moved and last_p > 0), elapsed or (moved and p < last_p)

    def _observation(self, t: float, p: float, r: int) -> tuple[list[float | int] | None, bool]:
        boundary, confirmed = self._classify(t, p, r)
        if self._latest:
            last_t, last_p, last_r, cycle = self._latest
            if t < last_t or (t == last_t and (p != last_p or r != last_r)):
                return None, False
            if not boundary and p < last_p:
                # A decrease without reset evidence is ambiguous. Reject it
                # rather than counting the recovery from a transient dip twice.
                return None, False
        else:
            cycle = 0
        return [t, p, r, cycle + int(boundary)], confirmed

    def _append(self, point: list[float | int]) -> None:
        self.points.append(point)
        self._dirty = True
        cutoff = point[0] - max(self.HORIZONS.values())
        while len(self.points) > 1 and self.points[1][0] <= cutoff:
            self.points.pop(0)
        self.points = self.points[-self.MAX_POINTS:]

    def _record(self, t: float, p: float, r: int) -> list[float | int] | None:
        point, confirmed = self._observation(t, p, r)
        if point is None:
            return None
        if confirmed and self._latest and self.points[-1] != self._latest:
            # Preserve the last known interval before the actual reset; only
            # the interval crossing its boundary is unknown.
            self._append(self._latest)
        if (not self.points or confirmed
                or t - self.points[-1][0] >= self.SAMPLE_SECONDS):
            self._append(point)
        elif self._latest and self.points[-1][3] == self._latest[3] and point[3] != self._latest[3]:
            # Persist the first uncertain boundary without adding a sample.
            # Further boundaries before the next sample remain one unknown
            # interval; a restart must never reconnect it to the old cycle.
            self._dirty = True
        self._latest = point
        return point

    def _project(self, point: list[float | int], *, record: bool) -> dict[str, int]:
        t, p, r, _ = point
        points = self.points if self.points and self.points[-1] == point else self.points + [point]
        # Warmup is a specific condition: an accepted live observation and
        # less than one hour of history. Missing coverage or rejected samples
        # are not progress toward a forecast and must not claim otherwise.
        if record and t - points[0][0] < self.MIN_HISTORY_SECONDS:
            self.status = "learning"
        fields = {}
        for name, horizon in self.HORIZONS.items():
            start = max(points[0][0], t - horizon)
            span = t - start
            if span < self.MIN_HISTORY_SECONDS:
                continue
            consumed = covered = 0.0
            for a, b in zip(points, points[1:]):
                duration = b[0] - a[0]
                overlap = b[0] - max(start, a[0])
                if overlap <= 0 or duration <= 0 or a[3] != b[3] or b[1] < a[1]:
                    continue
                # A whole same-cycle gap has a known total delta, including sleep.
                # Do not distribute a long unobserved gap across a window boundary.
                if overlap < duration and duration > self.MAX_INTERPOLATION_GAP:
                    continue
                covered += overlap
                consumed += (b[1] - a[1]) * overlap / duration
            if covered < self.MIN_HISTORY_SECONDS or covered < span * 0.8:
                continue
            # Replaying the same observation must not make the projection look
            # better as time passes, or extend its lifetime.
            forecast = p + consumed / covered * (r - t)
            # 101 is an overflow indicator, not a literal prediction of 101%.
            fields[name] = 101 if forecast > 100 else max(p, round(forecast))
            # Preserve precision around zero for the remaining-quota scale.
            # Saturate only outside its range, before rounding, so even a tiny
            # overshoot remains distinct from exactly +/-50 pp. Old fields above
            # remain available to devices running the previous firmware.
            remaining = 100 - forecast
            remaining_bp = (5100 if remaining > 50 else -5100 if remaining < -50
                            else round(remaining * 100))
            fields[name.replace("forecast", "remaining") + "_bp"] = remaining_bp
        if fields:
            fields["secondary_forecast_valid_until"] = min(r, int(t + self.TTL_SECONDS))
            self.status = "ready"
        return fields

    def packet_fields(self, snapshot: UsageSnapshot, now: float, key: list[Any],
                      *, record: bool = True) -> dict[str, int]:
        self.status = "unavailable"
        t = snapshot.quota_observed_at
        p, r = snapshot.secondary, snapshot.secondary_resets_at
        if t is None or not all(math.isfinite(v) for v in (t, p, r, now)):
            return {}
        if (not 0 <= now - t < self.TTL_SECONDS or not 0 <= p <= 100
                or not now < r <= t + 7 * 86400 + self.RESET_TOLERANCE_SECONDS):
            return {}
        if key != self.key:
            if not record:
                return {}
            self.key, self.points, self._latest = key, [], None
        if record:
            point = self._record(t, p, r)
            if point is None:
                return {}
            if self._needs_migration or self._dirty:
                self.save()
                self._dirty = self._needs_migration = False
        else:
            point, _ = self._observation(t, p, r)
            if point is None:
                return {}
        return self._project(point, record=record)


def forecast_packet_fields(args: argparse.Namespace, snapshot: UsageSnapshot) -> dict[str, Any]:
    unavailable = {"secondary_forecast_status": "unavailable"}
    if snapshot.quota_observed_at is None or snapshot.quota_account is None:
        return unavailable
    try:
        history = getattr(args, "_quota_forecast", None)
        if history is None:
            history = QuotaForecast(args.snapshot_cache_path.parent / "quota_history.json")
            args._quota_forecast = history
        key = [snapshot.limit_id, snapshot.quota_account]
        # Migrate the old file-metadata key only while it still matches the
        # current auth file. This check is never used as the new identity.
        earliest = 0
        if history.key != key:
            try:
                auth = (args.codex_home / "auth.json").stat()
                earliest = auth.st_mtime
                legacy_key = [snapshot.limit_id, [auth.st_ino, auth.st_mtime_ns, auth.st_size]]
                if history.key == legacy_key:
                    history.key = key
                    history.save()
            except FileNotFoundError:
                pass
        if snapshot.quota_live and not getattr(args, "_quota_seed_attempted", False):
            history.seed(snapshot, getattr(args, "_quota_log_samples", []), key, earliest)
            args._quota_seed_attempted = True
            args._quota_log_samples = []
        now = time.time()
        fields = history.packet_fields(snapshot, now, key, record=snapshot.quota_live)
        return {**fields, "secondary_forecast_status": history.status}
    except Exception as exc:
        # Optional analytics must never interrupt quota/state/approval delivery.
        if args.verbose:
            print(f"[forecast] unavailable: {type(exc).__name__}", file=sys.stderr)
        return unavailable


def window_is_available(reset_at: int, now: int | None = None) -> bool:
    return reset_at > (int(time.time()) if now is None else now)


def limit_matches(limit_id: str | None, preferred_limit_id: str) -> bool:
    value = str(limit_id or "")
    preferred = str(preferred_limit_id or "")
    return bool(value) and value == preferred


def snapshot_has_rate_limit(snapshot: UsageSnapshot, preferred_limit_id: str) -> bool:
    return limit_matches(snapshot.limit_id, preferred_limit_id) and window_is_available(
        snapshot.secondary_resets_at
    )


def snapshot_event_key(snapshot: UsageSnapshot) -> float:
    return snapshot.event_ts or 0.0


def stabilize_zero_quota(
    snapshot: UsageSnapshot,
    accepted: UsageSnapshot | None,
    pending: dict[str, Any],
    required_matches: int = 3,
) -> tuple[UsageSnapshot, bool]:
    is_zero = (
        snapshot.primary == 0
        and snapshot.secondary == 0
        and window_is_available(snapshot.primary_resets_at)
        and window_is_available(snapshot.secondary_resets_at)
    )
    accepted_is_zero = (
        accepted is not None
        and accepted.primary == 0
        and accepted.secondary == 0
        and window_is_available(accepted.primary_resets_at)
        and window_is_available(accepted.secondary_resets_at)
    )
    if not is_zero or accepted is None or accepted_is_zero:
        pending.clear()
        return snapshot, True

    pending["matches"] = int(pending.get("matches", 0)) + 1

    if pending["matches"] >= required_matches:
        pending.clear()
        return snapshot, True

    # The upstream usage endpoint occasionally emits an isolated empty 0/0 quota
    # snapshot. Keep the last accepted quota for the first two consecutive empty
    # snapshots, while still forwarding current activity and token information.
    stable = replace(
        accepted,
        tokens=snapshot.tokens,
        event_ts=snapshot.event_ts,
    )
    stable = attach_activity(stable, snapshot)
    return stable, False


def attach_activity(snapshot: UsageSnapshot, activity: UsageSnapshot) -> UsageSnapshot:
    return replace(
        snapshot,
        task_started_at=activity.task_started_at,
        task_complete_at=activity.task_complete_at,
        attention_at=activity.attention_at,
        dizzy_at=activity.dizzy_at,
        last_activity_at=activity.last_activity_at,
    )


def merge_latest_tokens(snapshot: UsageSnapshot, latest: UsageSnapshot | None) -> UsageSnapshot:
    if not latest or snapshot_event_key(latest) <= snapshot_event_key(snapshot):
        return snapshot
    return replace(
        snapshot,
        tokens=latest.tokens,
        source=latest.source,
        event_ts=latest.event_ts,
        task_started_at=latest.task_started_at,
        task_complete_at=latest.task_complete_at,
        attention_at=latest.attention_at,
        dizzy_at=latest.dizzy_at,
        last_activity_at=latest.last_activity_at,
    )


def snapshot_to_cache(snapshot: UsageSnapshot) -> dict[str, Any]:
    return {
        "version": 2,
        "tokens": snapshot.tokens,
        "primary": snapshot.primary,
        "secondary": snapshot.secondary,
        "primary_resets_at": snapshot.primary_resets_at,
        "secondary_resets_at": snapshot.secondary_resets_at,
        "source": str(snapshot.source),
        "event_ts": snapshot.event_ts,
        "limit_id": snapshot.limit_id,
        "limit_name": snapshot.limit_name,
        "quota_observed_at": snapshot.quota_observed_at,
        "quota_account": snapshot.quota_account,
        "account_checked": snapshot.account_checked,
        "auth_scope": snapshot.auth_scope,
        "gift_reset_expires_at": snapshot.gift_reset_expires_at,
        "gift_observed_at": snapshot.gift_observed_at,
    }


def finite_timestamp(value: Any) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def valid_percent(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100


def snapshot_from_cache(path: Path) -> UsageSnapshot | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(valid_percent(data.get(name)) for name in ("primary", "secondary")):
        return None
    try:
        # Legacy caches have no authoritative age. Do not infer it from file
        # mtime or activity, both of which can change without a new quota read.
        observed = finite_timestamp(data.get("quota_observed_at")) if data.get("version") == 2 else None
        gift_expiry = finite_timestamp(data.get("gift_reset_expires_at"))
        return UsageSnapshot(
            tokens=int(data.get("tokens") or 0),
            primary=clamp_percent(data.get("primary")),
            secondary=clamp_percent(data.get("secondary")),
            primary_resets_at=int(data.get("primary_resets_at") or 0),
            secondary_resets_at=int(data.get("secondary_resets_at") or 0),
            source=Path(data.get("source") or path),
            event_ts=float(data["event_ts"]) if data.get("event_ts") is not None else None,
            limit_id=data.get("limit_id"),
            limit_name=data.get("limit_name"),
            quota_observed_at=observed,
            quota_account=data.get("quota_account"),
            account_checked=data.get("account_checked") is True or data.get("quota_account") is not None,
            auth_scope=data.get("auth_scope"),
            gift_reset_expires_at=int(gift_expiry) if gift_expiry is not None else None,
            gift_observed_at=finite_timestamp(data.get("gift_observed_at")),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def save_snapshot_cache(snapshot: UsageSnapshot, path: Path = SNAPSHOT_CACHE_PATH) -> None:
    try:
        write_private_text(path, json.dumps(snapshot_to_cache(snapshot), separators=(",", ":")))
    except OSError:
        pass


def codex_cli_path(args: argparse.Namespace) -> Path | None:
    if args.codex_cli:
        return args.codex_cli
    found = shutil.which("codex")
    if found:
        return Path(found)
    if DEFAULT_CODEX_APP_CLI.exists():
        return DEFAULT_CODEX_APP_CLI
    return None


def auth_scope(args: argparse.Namespace) -> str:
    """Bind fallback data to a Codex home and its current authentication file.

    Metadata is only a conservative cache invalidation guard, not the account
    identity used by the forecast. Token refresh may invalidate fallback data;
    the next successful account/read restores it without losing history.
    """
    root = args.codex_home.expanduser().resolve()
    try:
        stat = (root / "auth.json").stat()
        revision = [stat.st_ino, stat.st_mtime_ns, stat.st_size]
    except OSError:
        revision = None
    return hashlib.sha256(json.dumps([str(root), revision]).encode()).hexdigest()


def gift_reset_snapshot(result: dict[str, Any], now: float) -> tuple[int | None, float | None]:
    """A complete credit list proves none; missing/malformed data proves nothing."""
    summary = result.get("rateLimitResetCredits")
    if not isinstance(summary, dict) or not isinstance(summary.get("credits"), list):
        return None, None
    expires = []
    for credit in summary["credits"]:
        if not isinstance(credit, dict) or not isinstance(credit.get("resetType"), str):
            return None, None
        if credit["resetType"] != "codexRateLimits":
            continue
        status = credit.get("status")
        if not isinstance(status, str):
            return None, None
        if status != "available":
            continue
        expiration = finite_timestamp(credit.get("expiresAt"))
        if expiration is None:
            return None, None
        if expiration > now:
            expires.append(int(expiration))
    return min(expires, default=0), now


def _rate_limit_window(window: Any) -> tuple[int | None, int, int]:
    if not isinstance(window, dict):
        return None, 0, 0

    raw_minutes = window.get("window_minutes")
    if raw_minutes is None:
        raw_minutes = window.get("windowDurationMins")
    try:
        minutes = int(raw_minutes) if raw_minutes is not None else None
    except (TypeError, ValueError, OverflowError):
        minutes = None

    used_percent = window.get("used_percent")
    if used_percent is None:
        used_percent = window.get("usedPercent")
    resets_at = window.get("resets_at")
    if resets_at is None:
        resets_at = window.get("resetsAt")
    try:
        reset = int(resets_at or 0)
    except (TypeError, ValueError, OverflowError):
        reset = 0
    return minutes, clamp_percent(used_percent), reset


def normalize_rate_limit_windows(rate_limits: dict[str, Any]) -> tuple[int, int, int, int]:
    windows = [
        (name, *_rate_limit_window(rate_limits.get(name)))
        for name in ("primary", "secondary")
        if isinstance(rate_limits.get(name), dict)
    ]

    primary_pct = primary_reset = secondary_pct = secondary_reset = 0
    has_duration = any(minutes is not None for _, minutes, _, _ in windows)

    # Window slots are not stable, so identify them by duration. This became
    # necessary on 2026-07-12, when the 6M-user reset temporarily omitted the
    # 300-minute window and placed the 10080-minute window in the primary slot.
    if has_duration:
        for _, minutes, percent, reset in windows:
            if minutes == PRIMARY_WINDOW_MINUTES:
                primary_pct, primary_reset = percent, reset
            elif minutes == SECONDARY_WINDOW_MINUTES:
                secondary_pct, secondary_reset = percent, reset
    elif len(windows) == 2:
        # Compatibility with older payloads that omitted window durations.
        _, _, primary_pct, primary_reset = windows[0]
        _, _, secondary_pct, secondary_reset = windows[1]

    return primary_pct, secondary_pct, primary_reset, secondary_reset


def rate_limit_percentages_valid(rate_limits: dict[str, Any]) -> bool:
    reported = [rate_limits[name] for name in ("primary", "secondary")
                if isinstance(rate_limits.get(name), dict)]
    percentages = [w.get("used_percent", w.get("usedPercent")) for w in reported]
    return bool(percentages) and all(valid_percent(v) for v in percentages)


def app_server_usage_snapshot_from_result(
    result: dict[str, Any],
    preferred_limit_id: str,
    activity: UsageSnapshot | None,
) -> UsageSnapshot | None:
    by_limit_id = result.get("rateLimitsByLimitId")
    rate_limits = None
    if isinstance(by_limit_id, dict):
        rate_limits = by_limit_id.get(preferred_limit_id)
    if not isinstance(rate_limits, dict):
        rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return None

    primary, secondary, primary_resets_at, secondary_resets_at = normalize_rate_limit_windows(
        rate_limits
    )

    snapshot = UsageSnapshot(
        tokens=activity.tokens if activity else 0,
        primary=primary,
        secondary=secondary,
        primary_resets_at=primary_resets_at,
        secondary_resets_at=secondary_resets_at,
        source=APP_SERVER_USAGE_SOURCE,
        event_ts=activity.event_ts if activity else None,
        limit_id=rate_limits.get("limitId") or preferred_limit_id,
        limit_name=rate_limits.get("limitName"),
    )
    # Only a successful, well-formed live response can extend forecast history.
    if not rate_limit_percentages_valid(rate_limits) or not snapshot_has_rate_limit(snapshot, preferred_limit_id):
        return None
    snapshot.quota_observed_at = time.time()
    snapshot.quota_live = True
    snapshot.gift_reset_expires_at, snapshot.gift_observed_at = gift_reset_snapshot(
        result, snapshot.quota_observed_at)
    if activity:
        snapshot = attach_activity(snapshot, activity)
    return snapshot


def forecast_account_identity(result: dict[str, Any] | None) -> str | None:
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return None
    email, plan = account.get("email"), account.get("planType")
    if not isinstance(email, str) or not email.strip() or not isinstance(plan, str) or plan == "unknown":
        return None
    # account/read exposes email + plan, not an opaque account/workspace ID.
    # Persist only their fingerprint, never the email or authentication tokens.
    identity = json.dumps([email.strip().lower(), plan], separators=(",", ":"))
    return "account-v1:" + hashlib.sha256(identity.encode()).hexdigest()


def read_app_server_usage(args: argparse.Namespace, activity: UsageSnapshot | None) -> UsageSnapshot | None:
    args._account_read = False
    args._usage_account = None
    args._gift_snapshot = (None, None)
    if args.no_appserver_usage:
        return None

    codex_cli = codex_cli_path(args)
    if not codex_cli or not codex_cli.exists():
        if args.verbose:
            print("[usage] Codex app-server CLI not found; falling back to rollout logs", file=sys.stderr)
        return None

    init_msg = {
        "id": "codex-usage-bridge-init",
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "codex-usage-ble-bridge",
                "title": "Codex Usage BLE Bridge",
                "version": "0.1",
            },
            "capabilities": {
                "experimentalApi": True,
                "requestAttestation": False,
                "optOutNotificationMethods": [],
            },
        },
    }
    read_msg = {
        "id": "codex-usage-rate-limits",
        "method": "account/rateLimits/read",
    }

    account_msg = {"id": "codex-usage-account", "method": "account/read",
                   "params": {"refreshToken": False}}
    proc: subprocess.Popen[str] | None = None
    stderr_text = ""
    quota_result = account_result = None
    account_received = quota_received = False
    try:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(args.codex_home)
        proc = subprocess.Popen(
            [str(codex_cli), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        for msg in (init_msg, account_msg, read_msg):
            proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + args.appserver_timeout
        buffer = b""
        while time.monotonic() < deadline and not (account_received and quota_received):
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == account_msg["id"]:
                    account_received, account_result = True, msg.get("result")
                elif msg.get("id") == read_msg["id"]:
                    quota_received, quota_result = True, msg.get("result")
        if account_received and isinstance(account_result, dict):
            args._account_read = True
            args._usage_account = forecast_account_identity(account_result)
        if isinstance(quota_result, dict):
            args._gift_snapshot = gift_reset_snapshot(quota_result, time.time())
            snapshot = app_server_usage_snapshot_from_result(quota_result, args.limit_id, activity)
            if snapshot is not None:
                snapshot.quota_account = forecast_account_identity(account_result)
                snapshot.account_checked = args._account_read
                snapshot.auth_scope = auth_scope(args)
            return snapshot
    except Exception as exc:
        if args.verbose:
            print(f"[usage] app-server usage unavailable: {exc}", file=sys.stderr)
        return None
    finally:
        if proc:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()
            if proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.5)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=0.5)
            with contextlib.suppress(Exception):
                if proc.stderr:
                    stderr_text = proc.stderr.read()
            if args.verbose and stderr_text:
                noisy = [
                    line
                    for line in stderr_text.splitlines()
                    if "warning: proceeding" not in line.lower()
                ]
                if noisy:
                    print("[usage] app-server stderr: " + " | ".join(noisy[-3:]), file=sys.stderr)

            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    if args.verbose:
        print("[usage] app-server rateLimits/read timed out; falling back to rollout logs", file=sys.stderr)
    return None


def clamp_percent(value: Any) -> int:
    try:
        n = round(float(value))
    except (TypeError, ValueError, OverflowError):
        n = 0
    return max(0, min(100, n))


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def tail_lines(path: Path, max_bytes: int) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # drop partial first line
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


def latest_rollout_paths(codex_home: Path, thread_id: str | None, limit: int) -> list[Path]:
    db = codex_home / "state_5.sqlite"
    if not db.exists():
        raise FileNotFoundError(f"Codex state database not found: {db}")

    con = sqlite3.connect(db)
    try:
        if thread_id:
            rows = con.execute(
                "select rollout_path from threads where id = ? limit 1",
                (thread_id,),
            ).fetchall()
        else:
            rows = con.execute(
                """
                select rollout_path
                from threads
                where rollout_path is not null and rollout_path != ''
                order by coalesce(updated_at_ms, updated_at * 1000) desc
                limit ?
                """,
                (limit,),
            ).fetchall()
    finally:
        con.close()

    paths: list[Path] = []
    for (raw,) in rows:
        p = Path(raw).expanduser()
        if p.exists():
            paths.append(p)
    return paths


def event_payload_text(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        return str(payload).lower()


def payload_wants_attention(payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type") or "").lower()
    if payload_type in ATTENTION_EVENT_TYPES:
        return True
    return (
        any(word in payload_type for word in ("approval", "permission", "confirm"))
        and "request" in payload_type
    )


def payload_looks_dizzy(payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type") or "").lower()
    if payload_type in DIZZY_EVENT_TYPES:
        return True
    if payload.get("rate_limit_reached_type"):
        return True
    if payload_type != "function_call_output":
        return False

    output = str(payload.get("output") or "").lower()
    if "process exited with code 0" in output[:300]:
        return False
    return any(
        word in output
        for word in ("rate_limit_reached", "rate limit", "fatal error", "traceback", "exception", "timed out")
    )


def extract_token_counts(path: Path, max_bytes: int) -> list[UsageSnapshot]:
    snapshots: list[UsageSnapshot] = []
    task_started_at: float | None = None
    task_complete_at: float | None = None
    attention_at: float | None = None
    dizzy_at: float | None = None
    last_activity_at: float | None = None

    for line in tail_lines(path, max_bytes):
        lower_line = line.lower()
        if not any(marker in lower_line for marker in INTERESTING_LINE_MARKERS):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        event_ts = parse_timestamp(event.get("timestamp"))

        if payload_type in {"token_count", "task_started", "task_complete"}:
            last_activity_at = newer_ts(last_activity_at, event_ts)
        if payload_type == "task_started":
            task_started_at = newer_ts(task_started_at, event_ts)
        elif payload_type == "task_complete":
            task_complete_at = newer_ts(task_complete_at, event_ts)

        if payload_wants_attention(payload):
            attention_at = newer_ts(attention_at, event_ts)
        if payload_looks_dizzy(payload):
            dizzy_at = newer_ts(dizzy_at, event_ts)

        if payload_type != "token_count":
            continue

        info = payload.get("info") or {}
        total_usage = info.get("total_token_usage") or {}
        rate_limits = payload.get("rate_limits") or {}
        primary, secondary, primary_resets_at, secondary_resets_at = normalize_rate_limit_windows(
            rate_limits
        )

        snapshot = UsageSnapshot(
            tokens=int(total_usage.get("total_tokens") or 0),
            primary=primary,
            secondary=secondary,
            primary_resets_at=primary_resets_at,
            secondary_resets_at=secondary_resets_at,
            source=path,
            event_ts=event_ts,
            limit_id=rate_limits.get("limit_id"),
            limit_name=rate_limits.get("limit_name"),
            quota_log_valid=rate_limit_percentages_valid(rate_limits),
        )
        snapshots.append(snapshot)

    for snapshot in snapshots:
        snapshot.task_started_at = task_started_at
        snapshot.task_complete_at = task_complete_at
        snapshot.attention_at = attention_at
        snapshot.dizzy_at = dizzy_at
        snapshot.last_activity_at = last_activity_at
    return snapshots


def choose_best_rate_limit_snapshot(
    snapshots: list[UsageSnapshot],
    preferred_limit_id: str,
    preferred_fresh_window: float = 180.0,
) -> UsageSnapshot | None:
    valid = [s for s in snapshots if s.quota_log_valid and snapshot_has_rate_limit(s, preferred_limit_id)]
    if not valid:
        return None

    latest_ts = max(snapshot_event_key(s) for s in valid)
    fresh = [s for s in valid if latest_ts - snapshot_event_key(s) <= preferred_fresh_window]
    exact = [s for s in fresh if s.limit_id == preferred_limit_id]
    return max(exact or fresh, key=snapshot_event_key)


def cached_usage(args: argparse.Namespace) -> UsageSnapshot | None:
    snapshot = getattr(args, "_last_usage", None)
    if snapshot is None:
        snapshot = snapshot_from_cache(args.snapshot_cache_path)
    if snapshot is None:
        return None
    if snapshot.limit_id != args.limit_id or snapshot.auth_scope != auth_scope(args):
        # Authentication metadata may change during logout or token refresh.
        # Discard the values, but preserve the fact that unscoped rollout data
        # is no longer an acceptable account fallback, including after restart.
        if snapshot.account_checked or snapshot.quota_account is not None:
            return replace(unavailable_usage(args), account_checked=True)
        return None
    return replace(snapshot, quota_live=False)


def unavailable_usage(args: argparse.Namespace, activity: UsageSnapshot | None = None) -> UsageSnapshot:
    snapshot = UsageSnapshot(
        tokens=activity.tokens if activity else 0,
        primary=0, secondary=0, primary_resets_at=0, secondary_resets_at=0,
        source=APP_SERVER_USAGE_SOURCE, event_ts=activity.event_ts if activity else None,
        limit_id=args.limit_id, limit_name=None, auth_scope=auth_scope(args),
        quota_account=getattr(args, "_usage_account", None),
        account_checked=getattr(args, "_account_read", False),
    )
    return attach_activity(snapshot, activity) if activity else snapshot


def quota_values(snapshot: UsageSnapshot) -> tuple[int, int, int, int]:
    return (snapshot.primary, snapshot.secondary,
            snapshot.primary_resets_at, snapshot.secondary_resets_at)


def read_usage(args: argparse.Namespace) -> UsageSnapshot:
    paths = latest_rollout_paths(args.codex_home, args.thread_id, args.thread_scan_limit)
    if args.rollout:
        paths.insert(0, args.rollout)

    snapshots: list[UsageSnapshot] = []
    seen: set[Path] = set()
    for path in paths:
        path = path.expanduser().resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        snapshots.extend(extract_token_counts(path, args.tail_bytes))

    latest_any = max(snapshots, key=snapshot_event_key) if snapshots else None
    if not getattr(args, "_quota_seed_attempted", False):
        args._quota_log_samples = snapshots

    cached = cached_usage(args)
    app_server_snapshot = read_app_server_usage(args, latest_any)
    account_checked = bool(getattr(args, "_account_read", False)
                           or (cached and (cached.account_checked or cached.quota_account is not None)))
    if (cached and getattr(args, "_account_read", False)
            and cached.quota_account != args._usage_account):
        cached = None
        args._last_usage = None

    if app_server_snapshot:
        app_server_snapshot.auth_scope = auth_scope(args)
        if cached and cached.quota_account != app_server_snapshot.quota_account:
            cached = None
        if cached is None:
            args._pending_zero_quota = {}
        pending = getattr(args, "_pending_zero_quota", {})
        args._pending_zero_quota = pending
        selected, accepted = stabilize_zero_quota(app_server_snapshot, cached, pending)
        if not accepted:
            selected = replace(selected, quota_live=False)
    else:
        # Rollout events can repeat a cached rate-limit payload on every task
        # update. They never train the forecast, and identical values cannot
        # make an already observed quota younger.
        args._pending_zero_quota = {}
        try:
            earliest = (args.codex_home / "auth.json").stat().st_mtime
        except OSError:
            earliest = 0
        # Logs do not identify the account. Once an API account is known, only
        # its authoritative cache is safe to replay while that source fails.
        allow_logs = not account_checked
        candidates = [s for s in snapshots if snapshot_event_key(s) >= earliest] if allow_logs else []
        best = choose_best_rate_limit_snapshot(candidates, args.limit_id)
        selected = cached
        if best and (cached is None or (
                snapshot_event_key(best) > (cached.quota_observed_at or 0)
                and quota_values(best) != quota_values(cached))):
            # Use the earliest copy of these values in this batch, not the
            # latest task activity timestamp carrying them.
            observed = min(snapshot_event_key(s) for s in candidates
                           if s.quota_log_valid and s.limit_id == best.limit_id
                           and quota_values(s) == quota_values(best))
            selected = replace(best, quota_observed_at=observed, quota_live=False,
                               quota_account=None, auth_scope=auth_scope(args))
        if selected is None:
            selected = unavailable_usage(args, latest_any)

    gift_expiry, gift_observed = getattr(args, "_gift_snapshot", (None, None))
    if gift_observed is not None:
        selected = replace(selected, gift_reset_expires_at=gift_expiry,
                           gift_observed_at=gift_observed)
    elif selected.gift_observed_at is None and cached is not None:
        selected = replace(selected, gift_reset_expires_at=cached.gift_reset_expires_at,
                           gift_observed_at=cached.gift_observed_at)
    selected = merge_latest_tokens(selected, latest_any)
    if latest_any:
        selected = attach_activity(selected, latest_any)
    selected = replace(selected, account_checked=account_checked or selected.account_checked)
    args._last_usage = selected
    save_snapshot_cache(selected, args.snapshot_cache_path)
    return selected


def choose_state(args: argparse.Namespace, snapshot: UsageSnapshot, tracker: ActivityTracker) -> str:
    if args.state != "auto":
        return args.state

    now = time.time()
    latest_start = snapshot.task_started_at or 0
    if is_recent(snapshot.attention_at, args.attention_window, now):
        return "attention"
    if (
        snapshot.task_complete_at
        and snapshot.task_complete_at >= latest_start
        and is_recent(snapshot.task_complete_at, args.completed_window, now)
    ):
        return "completed"
    # Dizzy is owned by the StickS3 IMU shake gesture, not by Codex logs.

    state = tracker.state_for(snapshot, args.busy_window)
    last_activity_at = snapshot.last_activity_at or snapshot.event_ts
    if state == "idle" and last_activity_at and now - last_activity_at >= args.sleep_window:
        return "sleep"
    return state


def short_text(value: Any, fallback: str, limit: int) -> str:
    text = str(value or fallback).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


class BleSession:
    def __init__(self, args: argparse.Namespace, client: BleakClient) -> None:
        self.args = args
        self.client = client
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._notify_buffer = ""
        self._loop = asyncio.get_running_loop()
        self._write_lock = asyncio.Lock()

    async def start_notify(self) -> None:
        try:
            await self.client.start_notify(NUS_TX_UUID, self._on_notify)
        except Exception as exc:
            print(f"[ble] notifications unavailable: {exc}", file=sys.stderr)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        self._notify_buffer += bytes(data).decode("utf-8", errors="replace")
        while "\n" in self._notify_buffer:
            raw, self._notify_buffer = self._notify_buffer.split("\n", 1)
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if self.args.verbose:
                    print(f"[ble] ignoring non-json notify: {line}", file=sys.stderr)
                continue
            self._loop.call_soon_threadsafe(self.incoming.put_nowait, msg)

    async def write_json(self, packet: dict[str, Any]) -> str:
        payload = (json.dumps(packet, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            for i in range(0, len(payload), self.args.chunk_size):
                chunk = payload[i:i + self.args.chunk_size]
                await asyncio.wait_for(
                    self.client.write_gatt_char(NUS_RX_UUID, chunk, response=not self.args.no_response),
                    timeout=self.args.ble_write_timeout,
                )
                await asyncio.sleep(self.args.chunk_delay)
        return payload.decode("utf-8").strip()


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


class CodexApprovalProxy:
    def __init__(self, args: argparse.Namespace, ble: BleSession) -> None:
        self.args = args
        self.ble = ble
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.pending_order: list[str] = []
        self.active_prompt_id: str | None = None
        self.next_prompt_num = 1
        self.enabled = False
        self.ipc_server: asyncio.AbstractServer | None = None

    def has_pending(self) -> bool:
        return bool(self.pending)

    async def start_ipc_server(self) -> None:
        sock = self.args.hook_approval_sock
        if not sock:
            return
        sock.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        try:
            self.ipc_server = await asyncio.start_unix_server(
                self._handle_ipc_client,
                path=str(sock),
            )
            with contextlib.suppress(OSError):
                sock.chmod(0o600)
            if self.args.verbose:
                print(f"[approval] hook IPC listening at {sock}", file=sys.stderr)
        except Exception as exc:
            print(f"[approval] hook IPC unavailable: {exc}", file=sys.stderr)

    async def close_ipc_server(self) -> None:
        if self.ipc_server:
            self.ipc_server.close()
            await self.ipc_server.wait_closed()
            self.ipc_server = None
        sock = self.args.hook_approval_sock
        if sock:
            with contextlib.suppress(FileNotFoundError):
                sock.unlink()

    async def _handle_ipc_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, Any]
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            request = json.loads(raw.decode("utf-8", errors="replace"))
            if request.get("type") != "permission_request":
                response = {"ok": False, "reason": "unsupported request"}
            else:
                timeout = float(request.get("timeout") or self.args.hook_approval_timeout)
                hook_payload = request.get("hook") or {}
                decision = await self.request_hook_permission(hook_payload, timeout)
                if decision:
                    response = {"ok": True, "decision": decision}
                else:
                    response = {"ok": False, "reason": "timeout"}
        except Exception as exc:
            response = {"ok": False, "reason": repr(exc)}

        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def inject_test_request(self) -> None:
        prompt_id = f"test{self.next_prompt_num}"
        self.next_prompt_num += 1
        self.pending[prompt_id] = {
            "method": "testApproval",
            "rpc_id": None,
            "params": {"reason": "A accept / B cancel"},
        }
        self.pending_order.append(prompt_id)
        if self.args.verbose:
            print(f"[approval] injected test request {prompt_id}", file=sys.stderr)
        if not self.active_prompt_id:
            await self._show_next_prompt()

    async def request_hook_permission(self, hook_payload: dict[str, Any], timeout: float) -> str | None:
        prompt_id = f"h{self.next_prompt_num}"
        self.next_prompt_num += 1
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending[prompt_id] = {
            "method": "hookPermissionRequest",
            "rpc_id": None,
            "params": hook_payload,
            "future": future,
        }
        self.pending_order.append(prompt_id)
        if self.args.verbose:
            tool = hook_payload.get("tool_name") if isinstance(hook_payload, dict) else None
            print(f"[approval] hook request {prompt_id}: {tool or 'permission'}", file=sys.stderr)
        if not self.active_prompt_id:
            await self._show_next_prompt()

        try:
            raw_decision = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._remove_pending(prompt_id)
            if self.active_prompt_id == prompt_id:
                self.active_prompt_id = None
                await self._show_next_prompt()
            if self.args.verbose:
                print(f"[approval] hook request {prompt_id} timed out", file=sys.stderr)
            return None

        if raw_decision == "accept":
            return "allow"
        if raw_decision == "cancel":
            return "deny"
        return None

    async def start(self) -> None:
        if self.args.no_approval_proxy:
            return

        codex_cli = self.args.codex_cli
        if not codex_cli:
            found = shutil.which("codex")
            codex_cli = Path(found) if found else DEFAULT_CODEX_APP_CLI
        if not codex_cli.exists():
            print(
                f"[approval] codex CLI not found at {codex_cli}; approval proxy disabled",
                file=sys.stderr,
            )
            return

        cmd = [str(codex_cli), "app-server", "proxy"]
        if self.args.approval_sock:
            cmd.extend(["--sock", str(self.args.approval_sock)])

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CODEX_HOME": str(self.args.codex_home)},
            )
        except FileNotFoundError:
            print("[approval] codex CLI not found; approval proxy disabled", file=sys.stderr)
            return
        except Exception as exc:
            print(f"[approval] could not start proxy: {exc}", file=sys.stderr)
            return

        self.enabled = True
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())
        await self._send_rpc(
            {
                "jsonrpc": "2.0",
                "id": "codex-usage-bridge-init",
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-usage-ble-bridge",
                        "title": "Codex Usage BLE Bridge",
                        "version": "0.1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "optOutNotificationMethods": [],
                    },
                },
            }
        )

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                self.enabled = False
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                if self.args.verbose:
                    print(f"[approval] non-json proxy output: {text}", file=sys.stderr)
                continue
            await self._handle_server_message(msg)

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if self.args.verbose or "Error:" in text or "failed" in text.lower():
                print(f"[approval] {text}", file=sys.stderr)

    async def _send_rpc(self, msg: dict[str, Any]) -> bool:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            self.enabled = False
            return False
        try:
            self.proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            self.enabled = False
            return False

    async def _handle_server_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method not in APPROVAL_METHODS or "id" not in msg:
            return

        prompt_id = f"a{self.next_prompt_num}"
        self.next_prompt_num += 1
        params = msg.get("params") or {}
        self.pending[prompt_id] = {
            "method": method,
            "rpc_id": msg["id"],
            "params": params,
        }
        self.pending_order.append(prompt_id)
        if self.args.verbose:
            print(f"[approval] request {prompt_id}: {method}", file=sys.stderr)
        if not self.active_prompt_id:
            await self._show_next_prompt()

    def _prompt_text(self, req: dict[str, Any]) -> tuple[str, str]:
        method = req["method"]
        params = req["params"]

        if method == "hookPermissionRequest":
            if not isinstance(params, dict):
                return "PERMISSION", "Codex permission request"
            tool = short_text(params.get("tool_name"), "PERMISSION", 19).upper()
            tool_input = params.get("tool_input")
            if isinstance(tool_input, dict):
                hint = (
                    tool_input.get("command")
                    or tool_input.get("cmd")
                    or tool_input.get("path")
                    or tool_input.get("file")
                    or tool_input.get("justification")
                    or tool_input.get("reason")
                )
                if isinstance(hint, list):
                    hint = " ".join(str(x) for x in hint)
            else:
                hint = tool_input
            if not hint:
                hint = params.get("cwd") or "Codex permission request"
            return tool, short_text(hint, "Codex permission request", 43)

        if method in {"item/commandExecution/requestApproval", "execCommandApproval"}:
            command = params.get("command") or ""
            if isinstance(command, list):
                command = " ".join(str(x) for x in command)
            return "COMMAND", short_text(params.get("reason") or command, "command approval", 43)

        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            hint = params.get("reason") or params.get("grantRoot") or "file change approval"
            return "FILE CHANGE", short_text(hint, "file change approval", 43)

        if method == "item/permissions/requestApproval":
            hint = params.get("reason") or "extra permissions"
            return "PERMISSIONS", short_text(hint, "extra permissions", 43)

        if method == "testApproval":
            return "TEST", short_text(params.get("reason"), "A accept / B cancel", 43)

        return "APPROVAL", "Codex approval"

    async def _show_next_prompt(self) -> None:
        if not self.pending_order:
            self.active_prompt_id = None
            await self.ble.write_json({"prompt": None})
            return

        prompt_id = self.pending_order[0]
        self.active_prompt_id = prompt_id
        tool, hint = self._prompt_text(self.pending[prompt_id])
        await self.ble.write_json(
            {
                "prompt": {
                    "id": prompt_id,
                    "tool": short_text(tool, "APPROVAL", 19),
                    "hint": short_text(hint, "Codex approval", 43),
                },
                "msg": "Codex approval",
            }
        )

    async def handle_device_message(self, msg: dict[str, Any]) -> None:
        if msg.get("cmd") != "permission":
            if self.args.verbose:
                print(f"[ble] notify {msg}", file=sys.stderr)
            return

        prompt_id = str(msg.get("id") or "")
        raw_decision = str(msg.get("decision") or "").lower()
        if raw_decision in {"accept", "approve", "approved", "once"}:
            decision = "accept"
        elif raw_decision in {"cancel", "deny", "denied", "decline", "abort"}:
            decision = "cancel"
        else:
            print(f"[approval] unknown decision from StickS3: {raw_decision}", file=sys.stderr)
            return

        req = self._remove_pending(prompt_id)
        if self.active_prompt_id == prompt_id:
            self.active_prompt_id = None

        if not req:
            print(f"[approval] no pending request for {prompt_id}", file=sys.stderr)
            await self._show_next_prompt()
            return

        if req["method"] == "testApproval":
            print(f"[approval] test decision from StickS3: {decision}", file=sys.stderr)
            await self._show_next_prompt()
            return

        if req["method"] == "hookPermissionRequest":
            future = req.get("future")
            if future and not future.done():
                future.set_result(decision)
            if self.args.verbose:
                print(f"[approval] hook decision {decision} for {prompt_id}", file=sys.stderr)
            await self._show_next_prompt()
            return

        response = self._response_for(req, decision)
        ok = await self._send_rpc(response)
        if self.args.verbose:
            status = "sent" if ok else "failed"
            print(f"[approval] {status} {decision} for {prompt_id}", file=sys.stderr)
        await self._show_next_prompt()

    def _remove_pending(self, prompt_id: str) -> dict[str, Any] | None:
        req = self.pending.pop(prompt_id, None)
        if prompt_id in self.pending_order:
            self.pending_order.remove(prompt_id)
        return req

    def _response_for(self, req: dict[str, Any], decision: str) -> dict[str, Any]:
        method = req["method"]
        rpc_id = req["rpc_id"]
        params = req["params"]

        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"decision": decision}}

        if method in {"execCommandApproval", "applyPatchApproval"}:
            legacy_decision = "approved" if decision == "accept" else "abort"
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"decision": legacy_decision}}

        if method == "item/permissions/requestApproval" and decision == "accept":
            requested = params.get("permissions") or {}
            granted = {
                key: requested[key]
                for key in ("network", "fileSystem")
                if requested.get(key) is not None
            }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "permissions": granted,
                    "scope": "turn",
                    "strictAutoReview": False,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32000,
                "message": "cancelled from StickS3",
            },
        }


async def find_device(name_filter: str, address: str | None, timeout: float):
    assert BleakScanner is not None
    devices = await BleakScanner.discover(
        timeout=timeout,
        service_uuids=[NUS_SERVICE_UUID],
    )
    service_filtered = bool(devices)
    if not devices:
        devices = await BleakScanner.discover(timeout=timeout)
    if getattr(find_device, "debug_scan", False):
        mode = "NUS" if service_filtered else "fallback"
        print(f"[scan] {mode} scan saw {len(devices)} device(s):", file=sys.stderr)
        for dev in devices:
            print(f"[scan]   {dev.name or '-'}  {dev.address}", file=sys.stderr)
    for dev in devices:
        dev_name = dev.name or ""
        if address and dev.address.lower() == address.lower():
            return dev
        if name_filter and name_filter in dev_name:
            return dev
        if name_filter.startswith("Codex") and "Claude" in dev_name:
            print(f"[scan] using cached old name: {dev_name}", file=sys.stderr)
            return dev
    if not address and name_filter and len(devices) == 1:
        dev = devices[0]
        print(
            f"[scan] using only NUS device despite cached name: {dev.name or dev.address}",
            file=sys.stderr,
        )
        return dev
    interesting = [
        d.name or d.address
        for d in devices
        if any(key in (d.name or "") for key in ("Codex", "Claude"))
    ]
    names = ", ".join(sorted(interesting)) or "none"
    raise RuntimeError(f"Codex BLE device not found. Saw: {names}")


async def send_packet(args: argparse.Namespace, packet: dict[str, Any]) -> None:
    assert BleakClient is not None
    dev = await find_device(args.name, args.address, args.scan_timeout)
    payload = (json.dumps(packet, separators=(",", ":")) + "\n").encode("utf-8")

    async with BleakClient(dev, timeout=args.connect_timeout) as client:
        if args.pair and hasattr(client, "pair"):
            try:
                await client.pair()
            except Exception as exc:  # macOS often pairs on encrypted write
                print(f"[pair] continuing after pair attempt failed: {exc}", file=sys.stderr)
        for i in range(0, len(payload), args.chunk_size):
            chunk = payload[i:i + args.chunk_size]
            await client.write_gatt_char(NUS_RX_UUID, chunk, response=not args.no_response)
            await asyncio.sleep(args.chunk_delay)


async def poll_usage(args: argparse.Namespace) -> UsageSnapshot:
    # Reserve half the update deadline for BLE delivery. A slow filesystem/API
    # poll keeps running once in the background; retries await that same task
    # instead of spawning competing threads or tearing down the BLE session.
    pending = getattr(args, "_usage_poll", None)
    if pending is None:
        pending = asyncio.create_task(asyncio.to_thread(read_usage, args))
        args._usage_poll = pending
    try:
        return await asyncio.wait_for(asyncio.shield(pending),
                                      timeout=getattr(args, "update_timeout", 20) / 2)
    finally:
        if pending.done():
            args._usage_poll = None


async def send_usage_update(
    args: argparse.Namespace,
    tracker: ActivityTracker,
    ble: BleSession | None = None,
    approvals: CodexApprovalProxy | None = None,
) -> None:
    try:
        snapshot = await poll_usage(args)
    except Exception as exc:
        # An unreadable source is a data problem, not a broken BLE connection.
        # Keep sending honest status so the display can explain the failure.
        if args.verbose:
            print(f"[usage] poll failed: {type(exc).__name__}", file=sys.stderr)
        snapshot = cached_usage(args) or unavailable_usage(args)

    state = choose_state(args, snapshot, tracker)
    if approvals and approvals.has_pending():
        state = "attention"

    packet = snapshot.packet(state)
    packet.update(forecast_packet_fields(args, snapshot))
    line = json.dumps(packet, separators=(",", ":"))

    if args.dry_run:
        print(line)
        touch_heartbeat(args, "dry_run")
    elif ble:
        await ble.write_json(packet)
        touch_heartbeat(args, "sent")
        if args.verbose:
            age = "?"
            if snapshot.event_ts is not None:
                age = f"{int(time.time() - snapshot.event_ts)}s"
            print(
                f"sent {line} from {snapshot.source.name} "
                f"limit={snapshot.limit_id or '-'} age={age}",
                flush=True,
            )


async def bridge_loop(args: argparse.Namespace) -> None:
    setattr(find_device, "debug_scan", args.debug_scan)
    tracker = ActivityTracker()
    if args.dry_run:
        while True:
            await asyncio.wait_for(send_usage_update(args, tracker), timeout=args.update_timeout)
            if args.once:
                return
            await asyncio.sleep(args.interval)

    assert BleakClient is not None
    dev = await find_device(args.name, args.address, args.scan_timeout)
    async with BleakClient(dev, timeout=args.connect_timeout) as client:
        if args.pair and hasattr(client, "pair"):
            try:
                await client.pair()
            except Exception as exc:  # macOS often pairs on encrypted write
                print(f"[pair] continuing after pair attempt failed: {exc}", file=sys.stderr)

        ble = BleSession(args, client)
        await ble.start_notify()
        approvals = CodexApprovalProxy(args, ble)
        await approvals.start_ipc_server()
        await approvals.start()
        if args.test_approval:
            await approvals.inject_test_request()

        async def usage_runner() -> None:
            while True:
                await asyncio.wait_for(
                    send_usage_update(args, tracker, ble, approvals),
                    timeout=args.update_timeout,
                )
                if args.once:
                    return
                await asyncio.sleep(args.interval)

        async def device_runner() -> None:
            while True:
                msg = await ble.incoming.get()
                await approvals.handle_device_message(msg)

        try:
            if args.once:
                await usage_runner()
                return
            await asyncio.gather(usage_runner(), device_runner())
        finally:
            await approvals.close_ipc_server()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bridge Codex usage to a StickS3 over BLE.",
    )
    p.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    p.add_argument("--rollout", type=Path, help="Read a specific Codex rollout JSONL")
    p.add_argument("--thread-id", help="Read a specific Codex thread from state_5.sqlite")
    p.add_argument("--thread-scan-limit", type=int, default=12)
    p.add_argument("--tail-bytes", type=int, default=8 * 1024 * 1024)
    p.add_argument("--limit-id", default="codex", help="Prefer this rate_limits.limit_id")
    p.add_argument(
        "--no-appserver-usage",
        action="store_true",
        help="Disable account/rateLimits/read and use rollout logs only",
    )
    p.add_argument(
        "--appserver-timeout",
        type=float,
        default=4.0,
        help="Seconds to wait for account/rateLimits/read",
    )

    p.add_argument("--name", default="Codex-", help="BLE device name substring")
    p.add_argument("--address", help="BLE address/UUID if name scan is not enough")
    p.add_argument("--scan-timeout", type=float, default=8.0)
    p.add_argument("--debug-scan", action="store_true", help="Print raw BLE scan results")
    p.add_argument("--connect-timeout", type=float, default=20.0)
    p.add_argument("--ble-write-timeout", type=float, default=8.0)
    p.add_argument("--update-timeout", type=float, default=20.0)
    p.add_argument("--heartbeat-path", type=Path)
    p.add_argument("--no-response", action="store_true", help="Use write-without-response")
    p.add_argument("--pair", action="store_true", help="Try explicit BLE pairing first")
    p.add_argument("--chunk-size", type=int, default=20, help="BLE write chunk size")
    p.add_argument("--chunk-delay", type=float, default=0.02, help="Delay between BLE chunks")
    p.add_argument(
        "--no-approval-proxy",
        action="store_true",
        help="Disable Codex app-server approval proxy integration",
    )
    p.add_argument(
        "--approval-sock",
        type=Path,
        help="Optional Codex app-server control socket for approval proxy",
    )
    p.add_argument(
        "--codex-cli",
        type=Path,
        help="Path to the Codex CLI used for app-server proxy",
    )
    p.add_argument(
        "--test-approval",
        action="store_true",
        help="Send a fake approval prompt to the StickS3 and print the A/B decision",
    )
    p.add_argument(
        "--hook-approval-sock",
        type=Path,
        default=None,
        help="Unix socket used by PermissionRequest hooks to ask the StickS3",
    )
    p.add_argument(
        "--hook-approval-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for A/B on hardware approval requests",
    )

    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--state",
        default="auto",
        choices=["auto", "idle", "busy", "attention", "completed", "celebrate", "dizzy", "heart", "sleep"],
    )
    p.add_argument("--busy-window", type=float, default=60.0)
    p.add_argument("--completed-window", type=float, default=25.0)
    p.add_argument("--attention-window", type=float, default=120.0)
    p.add_argument("--dizzy-window", type=float, default=60.0)
    p.add_argument("--sleep-window", type=float, default=20 * 60.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.codex_home = args.codex_home.expanduser()
    args.state_dir = args.codex_home / "codex-usage-bridge"
    args.snapshot_cache_path = args.state_dir / "last_usage_snapshot.json"
    if args.rollout:
        args.rollout = args.rollout.expanduser()
    if args.approval_sock:
        args.approval_sock = args.approval_sock.expanduser()
    if args.hook_approval_sock:
        args.hook_approval_sock = args.hook_approval_sock.expanduser()
    else:
        args.hook_approval_sock = args.state_dir / "approval.sock"
    if args.heartbeat_path:
        args.heartbeat_path = args.heartbeat_path.expanduser()
    else:
        args.heartbeat_path = args.state_dir / "bridge.heartbeat"
    if args.codex_cli:
        args.codex_cli = args.codex_cli.expanduser()
    ensure_private_dir(args.state_dir)

    if not args.dry_run and (BleakClient is None or BleakScanner is None):
        print("Missing dependency: bleak. Install with `python3 -m pip install bleak`.", file=sys.stderr)
        return 2

    try:
        asyncio.run(bridge_loop(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"codex_usage_ble_bridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
