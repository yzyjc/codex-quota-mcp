"""Read-only MCP bridge for Codex App Server rate limits.

The server speaks MCP over stdio and starts a short-lived local
``codex app-server`` process for each quota request.  It deliberately does
not read, copy, or print authentication tokens.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SERVER_NAME = "codex-quota"
SERVER_VERSION = "0.2.0"
MAX_OBSERVATIONS = 64
MIN_REPORT_INTERVAL_SECONDS = 10 * 60
REPORT_CHANGE_THRESHOLD_PP = 3
APP_SERVER_TIMEOUT_SECONDS = 15
HISTORY_LOCK_TIMEOUT_SECONDS = 5


def state_path() -> Path:
    configured = os.environ.get("CODEX_QUOTA_STATE")
    if configured:
        return Path(configured)
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / "CodexQuotaMCP" / "history.json"


@contextmanager
def history_lock() -> Iterator[None]:
    """Serialize history read/modify/write operations across MCP processes."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + HISTORY_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for quota history lock")
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_observations_unlocked() -> list[dict[str, Any]]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("observations", [])
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def load_observations() -> list[dict[str, Any]]:
    with history_lock():
        return _load_observations_unlocked()


def append_observation(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Atomically read the prior report and append one bounded snapshot."""
    with history_lock():
        observations = _load_observations_unlocked()
        previous_report = next((item for item in reversed(observations) if item.get("reported")), None)
        observations = (observations + [snapshot])[-MAX_OBSERVATIONS:]
        path = state_path()
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps({"observations": observations}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return previous_report, observations


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Stop the App Server and any wrapper child process on Windows."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def app_server_request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one read-only App Server request and return its JSON-RPC result."""
    command = os.environ.get("CODEX_COMMAND", "codex")
    timeout_seconds = float(os.environ.get("CODEX_APP_SERVER_TIMEOUT_SECONDS", APP_SERVER_TIMEOUT_SECONDS))
    proc = subprocess.Popen(
        [command, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=os.environ.copy(),
    )
    # Codex App Server uses JSON-RPC-shaped JSONL, but omits the jsonrpc field
    # on the wire (unlike MCP, which keeps it).
    messages = [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": SERVER_NAME, "title": "Codex Quota MCP", "version": SERVER_VERSION},
            "capabilities": {},
        }},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": method, "params": params or {}},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    stdout_queue: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                stdout_queue.put(line)
        finally:
            stdout_queue.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.flush()
        deadline = time.monotonic() + timeout_seconds
        result: dict[str, Any] | None = None
        raw_lines: list[str] = []
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                line = stdout_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            raw_lines.append(line)
            if not line.strip():
                continue
            try:
                message_data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message_data.get("id") == 2:
                if "error" in message_data:
                    terminate_process_tree(proc)
                    stderr_text = proc.stderr.read() if proc.stderr else ""
                    detail = (stderr_text or "").strip()[-1000:]
                    suffix = f"; stderr: {detail}" if detail else ""
                    raise RuntimeError(message_data["error"].get("message", "App Server request failed") + suffix)
                result = message_data.get("result", {})
                break
        if result is None:
            terminate_process_tree(proc)
            stderr_text = proc.stderr.read() if proc.stderr else ""
            detail = (stderr_text or "").strip()[-1000:]
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Codex App Server timed out or returned no response within {timeout_seconds:g}s{suffix}")
        return result
    finally:
        terminate_process_tree(proc)
        if proc.stdin:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        reader.join(timeout=0.2)


def iso_time(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def normalize_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    return {
        "used_percent": used,
        "remaining_percent": (100 - used) if isinstance(used, (int, float)) else None,
        "window_minutes": window.get("windowDurationMins"),
        "resets_at": window.get("resetsAt"),
        "resets_at_iso": iso_time(window.get("resetsAt")),
    }


def sliding_burn(observations: list[dict[str, Any]], current: dict[str, Any], window_minutes: int) -> dict[str, Any]:
    """Calculate observed burn from samples inside one time window."""
    now = current["timestamp"]
    cutoff = now - window_minutes * 60
    candidates = [
        item for item in observations
        if isinstance(item.get("timestamp"), (int, float))
        and item["timestamp"] >= cutoff
        and isinstance(item.get("primary_used_percent"), (int, float))
    ]
    if candidates and isinstance(current.get("primary_used_percent"), (int, float)):
        oldest = min(candidates, key=lambda item: item["timestamp"])
        observed_minutes = (now - oldest["timestamp"]) / 60
        delta = current["primary_used_percent"] - oldest["primary_used_percent"]
        if observed_minutes > 0 and delta >= 0:
            return {
                "consumed_percent": round(delta, 2),
                "observed_minutes": round(observed_minutes, 1),
                "sample_count": len(candidates),
                "consumed_percent_per_hour": round(delta * 60 / observed_minutes, 2),
            }
    return {"consumed_percent": None, "observed_minutes": 0, "sample_count": len(candidates)}


def build_brief(observations: list[dict[str, Any]], current: dict[str, Any], *, model: str | None, context: Any) -> dict[str, Any]:
    """Create only the short telemetry packet; the Agent owns the decision."""
    return {
        "resource_brief": {
            "primary_remaining_percent": current.get("primary_remaining_percent"),
            "primary_reset_at": current.get("primary_reset_at_iso"),
            "primary_window_minutes": current.get("primary_window_minutes"),
            "weekly_remaining_percent": current.get("secondary_remaining_percent"),
            "weekly_reset_at": current.get("secondary_reset_at_iso"),
            "weekly_window_minutes": current.get("secondary_window_minutes"),
            "observed_consumption": {
                "5m": sliding_burn(observations, current, 5),
                "15m": sliding_burn(observations, current, 15),
                "30m": sliding_burn(observations, current, 30),
            },
            "model": model,
            "context_used_percent": context,
        },
        "telemetry_only": True,
        "telemetry_basis": "Observed changes between MCP queries; not per-turn token telemetry.",
        "resource_planning_policy": (
            "Use a very small reasoning budget. Do not perform extended quota analysis, "
            "forecasting, or numerical optimization. Read this telemetry, make the minimum "
            "local decision needed, and immediately return to the primary task."
        ),
    }


def select_windows(mode: str) -> list[str]:
    if mode == "5m":
        return ["5m"]
    if mode == "30m":
        return ["30m"]
    if mode == "task_start":
        return ["5m", "15m", "30m"]
    return ["15m"]


def get_quota(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    mode = arguments.get("mode") if arguments.get("mode") in {"5m", "15m", "30m", "task_start"} else "15m"
    model = arguments.get("model") if isinstance(arguments.get("model"), str) else None
    context = arguments.get("context_used_percent")
    if not isinstance(context, (int, float)):
        context = None
    result = app_server_request("account/rateLimits/read")
    limits = result.get("rateLimits", {}) if isinstance(result, dict) else {}
    primary = normalize_window(limits.get("primary"))
    secondary = normalize_window(limits.get("secondary"))
    now = datetime.now(timezone.utc)
    observations = load_observations()
    previous_report = next((item for item in reversed(observations) if item.get("reported")), None)
    snapshot = {
        "timestamp": now.timestamp(),
        "retrieved_at": now.isoformat(),
        "primary_used_percent": primary.get("used_percent") if primary else None,
        "secondary_used_percent": secondary.get("used_percent") if secondary else None,
        "primary_remaining_percent": primary.get("remaining_percent") if primary else None,
        "secondary_remaining_percent": secondary.get("remaining_percent") if secondary else None,
        "primary_reset_at_iso": primary.get("resets_at_iso") if primary else None,
        "primary_window_minutes": primary.get("window_minutes") if primary else None,
        "secondary_reset_at_iso": secondary.get("resets_at_iso") if secondary else None,
        "secondary_window_minutes": secondary.get("window_minutes") if secondary else None,
    }
    elapsed = now.timestamp() - previous_report.get("timestamp", 0) if previous_report else None
    primary_delta = None
    if previous_report and isinstance(previous_report.get("primary_used_percent"), (int, float)) and isinstance(snapshot.get("primary_used_percent"), (int, float)):
        primary_delta = snapshot["primary_used_percent"] - previous_report["primary_used_percent"]
    reset_changed = bool(previous_report and snapshot.get("primary_reset_at_iso") != previous_report.get("primary_reset_at_iso"))
    report_due = (
        mode == "task_start"
        or (
        previous_report is None
        or (isinstance(elapsed, (int, float)) and elapsed >= MIN_REPORT_INTERVAL_SECONDS)
        or (isinstance(primary_delta, (int, float)) and abs(primary_delta) >= REPORT_CHANGE_THRESHOLD_PP)
        or reset_changed
        )
    )
    snapshot["reported"] = report_due
    _, observations = append_observation(snapshot)
    response = {
        "source": "codex-app-server",
        "read_only": True,
        "retrieved_at": now.isoformat(),
        "mode": mode,
        "report_due": report_due,
        "next_report_after_seconds": (
            max(0, int(MIN_REPORT_INTERVAL_SECONDS - elapsed))
            if not report_due and isinstance(elapsed, (int, float))
            else (MIN_REPORT_INTERVAL_SECONDS if not report_due else None)
        ),
    }
    if report_due:
        brief = build_brief(observations, {
            **snapshot,
            "primary_remaining_percent": primary.get("remaining_percent") if primary else None,
            "secondary_remaining_percent": secondary.get("remaining_percent") if secondary else None,
            "primary_reset_at_iso": primary.get("resets_at_iso") if primary else None,
            "primary_window_minutes": primary.get("window_minutes") if primary else None,
            "secondary_reset_at_iso": secondary.get("resets_at_iso") if secondary else None,
            "secondary_window_minutes": secondary.get("window_minutes") if secondary else None,
        }, model=model, context=context)
        windows = brief["resource_brief"]["observed_consumption"]
        brief["resource_brief"]["observed_consumption"] = {
            key: windows[key] for key in select_windows(mode)
        }
        response["brief_report"] = brief
    return response


TOOLS = [{
    "name": "get_codex_quota",
    "description": "Read low-frequency, model-agnostic Codex quota telemetry. Modes: 5m, 15m, 30m return one sliding window; task_start returns all three windows and bypasses throttling. At the beginning of every new task or user request, call exactly once with mode=task_start, even when the same conversation continues. During ongoing work, use a window mode and respect report_due=false. The Agent must choose any action itself with a tiny reasoning budget; this tool never recommends an action or performs forecasting. Read-only; no tokens are returned.",
    "inputSchema": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["5m", "15m", "30m", "task_start"], "description": "Use task_start exactly once at the beginning of every new task or user request; otherwise choose one observation window."},
        "model": {"type": "string", "description": "Optional active model name, only if known by the caller."},
        "context_used_percent": {"type": "number", "minimum": 0, "maximum": 100, "description": "Optional current context usage, only if known by the caller."},
    }, "additionalProperties": False},
}]


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = message.get("params", {}).get("name")
        if name != "get_codex_quota":
            error(request_id, -32602, f"Unknown tool: {name}")
            return
        try:
            payload = get_quota(message.get("params", {}).get("arguments"))
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "structuredContent": payload,
            }})
        except Exception as exc:
            error(request_id, -32000, f"Unable to read Codex quota: {exc}")
    elif method == "notifications/initialized":
        return
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    else:
        error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        if line.strip():
            try:
                handle(json.loads(line))
            except json.JSONDecodeError as exc:
                error(None, -32700, f"Invalid JSON: {exc}")


if __name__ == "__main__":
    main()

