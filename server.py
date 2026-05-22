# /// script
# dependencies = [
#   "fastmcp",
#   "google-genai",
#   "obsws-python",
#   "websocket-client",
#   "pyobjc-framework-Quartz; sys_platform == 'darwin'",
# ]
# ///
"""Video Feeder — record QA sessions via OBS and analyze them with Gemini."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from fastmcp import FastMCP
except Exception:  # pragma: no cover - lets pure helper tests run without MCP deps
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, _name: str) -> None:
            pass

        def tool(self):
            def decorator(func):
                return func

            return decorator

        def run(self) -> None:
            raise RuntimeError("fastmcp is not installed")

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - lets pure helper tests run without Gemini deps
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

mcp = FastMCP("video-feeder")


# ---------------------------------------------------------------------------
# Constants and process-local state
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(
    os.environ.get("VIDEO_FEEDER_APPS_CONFIG", "~/.config/video-feeder/apps.json")
).expanduser()
RUNS_DIR = Path(os.environ.get("VIDEO_FEEDER_RUNS_DIR", "~/.qa-runs/video-feeder")).expanduser()

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
QA_PROFILE = os.environ.get("VIDEO_FEEDER_OBS_PROFILE", "Video Feeder QA")
QA_SCENE_COLLECTION = os.environ.get("VIDEO_FEEDER_OBS_SCENE_COLLECTION", "Video Feeder QA")
QA_SCENE = os.environ.get("VIDEO_FEEDER_OBS_SCENE", "Video Feeder")
QA_SOURCE = os.environ.get("VIDEO_FEEDER_OBS_SOURCE", "VF Capture")
DEFAULT_CAPTURE_MAX_SECONDS = int(os.environ.get("VIDEO_FEEDER_MAX_CAPTURE_SECONDS", "300"))
PROMPT_TEXT_BUDGET = int(os.environ.get("VIDEO_FEEDER_PROMPT_TEXT_BUDGET", "60000"))
GEMINI_PROCESSING_TIMEOUT_SECONDS = int(
    os.environ.get("VIDEO_FEEDER_GEMINI_PROCESSING_TIMEOUT_SECONDS", "300")
)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".gif"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
}

WITNESS_PROMPT = """\
You are a witness. A developer recorded their screen and narrated while \
reproducing a bug. Your ONLY job is to report exactly what you saw and heard. \
Do NOT guess at code causes, suggest fixes, or speculate about implementation.

You may also receive a QA evidence packet with logs, CDP events, and context.

Report in this structure:

## Transcript
Transcribe the developer's spoken narration with timestamps. Include every \
meaningful utterance — pauses, self-corrections, and emphasis matter.

## Timeline
Step-by-step description of what happened on screen, with video timestamps:
- What the developer clicked, typed, or navigated to
- What the UI showed in response
- Any visible errors, loading states, flickers, or unexpected behavior
- Any lag, freezes, or timing issues

## Log Correlations
Match visible events to log/CDP entries by timestamp. Quote the relevant \
log lines. Note any log events that have NO visible UI counterpart and vice versa.

## Observations
What appeared to go wrong, stated as observations not diagnoses:
- "The button was clicked at 0:12 but nothing happened until 0:18"
- "An error toast appeared saying X but the network request returned 200"
- "The developer said 'this should show Y' but Z was displayed instead"

Do NOT suggest root causes or code to inspect. An AI coding assistant that \
knows the codebase will read this report and investigate from there."""

BUG_PROMPT = """\
You are a senior software engineer helping debug an issue that another developer \
recorded because they couldn't describe it in words.

The recording may include microphone narration. Treat the spoken narration as \
important context and correlate it with the screen recording.

You may also receive a compact QA evidence packet:
- manifest metadata with wall-clock capture timestamps
- scoped app logs sliced to the capture interval
- optional Chrome DevTools Protocol events
- current work context and repository metadata

Your job:
1. Describe exactly what you see happening, step by step with video timestamps
2. Correlate visible behavior with any log/CDP events by timestamp
3. Identify what appears to be the bug or unexpected behavior
4. Note error messages, console output, network failures, or UI state
5. Suggest likely root causes and the first code surfaces to inspect
6. Ask 2-3 targeted follow-up questions only if needed

Be concrete and specific. The developer is going to relay your analysis to an AI \
coding assistant, so structure your response to be actionable."""

FOLLOWUP_PROMPT = """\
You are a witness being asked a follow-up question about a screen recording you \
already watched. The video is attached again for reference.

Your previous witness report is included below. Answer the question using ONLY \
what you can see and hear in the recording and the attached evidence. Be specific \
with timestamps. Do NOT guess at code causes.

## Previous Witness Report
{previous_report}

## Question
{question}"""

_obs = None
_active_sessions: dict[str, dict[str, Any]] = {}
_run_locks: dict[str, threading.RLock] = {}
_state_lock = threading.Lock()
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GENERATED_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[A-Za-z0-9_-]+$")
_TERMINAL_STATUSES = {"analyzed", "reported", "analysis_failed", "failed"}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _now_epoch() -> float:
    return time.time()


def _iso(epoch: float | None = None) -> str:
    timestamp = _now_epoch() if epoch is None else epoch
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def _new_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _run_dir(run_id: str) -> Path:
    _validate_run_id(run_id)
    return RUNS_DIR / run_id


def _manifest_path(run_id: str) -> Path:
    return _run_dir(run_id) / "manifest.json"


def _load_manifest(run_id: str) -> dict[str, Any]:
    _validate_run_id(run_id)
    path = _manifest_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"No Video Feeder run found for run_id: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(manifest: dict[str, Any]) -> None:
    run_id = manifest["run_id"]
    with _lock_for_run(run_id):
        path = _manifest_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _append_warning(manifest: dict[str, Any], message: str) -> None:
    manifest.setdefault("warnings", []).append(message)


def _latest_run_id(*, include_reported: bool = False) -> str | None:
    with _state_lock:
        for run_id, session in reversed(_active_sessions.items()):
            status = session.get("status")
            if include_reported and status == "reported":
                return run_id
            if status not in _TERMINAL_STATUSES:
                return run_id
    if include_reported and RUNS_DIR.is_dir():
        candidates = []
        for manifest_path in RUNS_DIR.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("status") == "reported" and manifest.get("gemini_file_ref"):
                    candidates.append((manifest_path.stat().st_mtime, manifest["run_id"]))
            except Exception:
                continue
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
    return None


def _resolve_run_id(run_id: str = "", *, include_reported: bool = False) -> str:
    if run_id:
        return _validate_run_id(run_id)
    latest = _latest_run_id(include_reported=include_reported)
    if not latest:
        raise FileNotFoundError("No active or saved Video Feeder run found")
    return latest


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"Invalid run_id {run_id!r}; use only letters, numbers, '_' and '-'")
    return run_id


def _looks_like_generated_run_id(value: str) -> bool:
    return bool(_GENERATED_RUN_ID_RE.fullmatch(value))


def _lock_for_run(run_id: str) -> threading.RLock:
    run_id = _validate_run_id(run_id)
    with _state_lock:
        lock = _run_locks.get(run_id)
        if lock is None:
            lock = threading.RLock()
            _run_locks[run_id] = lock
        return lock


def _set_active_status(run_id: str, status: str) -> None:
    with _state_lock:
        if run_id in _active_sessions:
            _active_sessions[run_id]["status"] = status


def _release_active_session(manifest: dict[str, Any]) -> None:
    run_id = manifest["run_id"]
    with _state_lock:
        session = _active_sessions.pop(run_id, None)
    if session and session.get("process") is not None:
        process = session["process"]
        manifest["app_process_left_running"] = process.poll() is None


def _expand_path(value: str, cwd: str | Path | None = None) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute() and cwd:
        expanded = Path(cwd).expanduser() / expanded
    return expanded.resolve()


def _read_text_excerpt(path: Path, max_chars: int) -> str:
    if not path.exists():
        return f"[missing: {path}]"
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) <= max_chars:
        return data
    keep_head = max_chars // 3
    keep_tail = max_chars - keep_head
    return (
        data[:keep_head]
        + f"\n\n[... truncated {len(data) - max_chars} chars from middle of {path.name} ...]\n\n"
        + data[-keep_tail:]
    )


def _safe_json_text(value: Any, max_chars: int) -> str:
    text = json.dumps(value, indent=2, sort_keys=True)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated ...]"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _normalize_apps_config(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict) and "apps" in raw:
        raw_apps = raw["apps"]
    else:
        raw_apps = raw

    apps: dict[str, dict[str, Any]] = {}
    if isinstance(raw_apps, dict):
        for name, config in raw_apps.items():
            if not isinstance(config, dict):
                raise ValueError(f"App config for {name!r} must be an object")
            app = dict(config)
            app.setdefault("name", name)
            apps[name] = app
    elif isinstance(raw_apps, list):
        for config in raw_apps:
            if not isinstance(config, dict) or not config.get("name"):
                raise ValueError("List-form app configs must be objects with a name")
            apps[str(config["name"])] = dict(config)
    else:
        raise ValueError("apps.json must be an app mapping, an app list, or {'apps': ...}")

    for name, app in apps.items():
        if not app.get("cwd"):
            raise ValueError(f"App {name!r} is missing cwd")
        if not app.get("command"):
            raise ValueError(f"App {name!r} is missing command")
        if not app.get("window_match"):
            raise ValueError(f"App {name!r} is missing window_match")
    return apps


def _load_apps_config(path: Path = CONFIG_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return _normalize_apps_config(json.loads(path.read_text(encoding="utf-8")))


@mcp.tool()
def list_apps() -> str:
    """List configured QA apps from ~/.config/video-feeder/apps.json."""
    try:
        apps = _load_apps_config()
    except Exception as exc:
        return f"Error loading {CONFIG_PATH}: {exc}"

    if not apps:
        return (
            f"No apps configured. Create {CONFIG_PATH} with entries like:\n"
            '{\n  "apps": {\n    "orchid": {\n'
            '      "cwd": "/path/to/app",\n'
            '      "command": "npm run tauri dev",\n'
            '      "window_match": "Orchid",\n'
            '      "cdp_url": "http://localhost:9222"\n'
            "    }\n  }\n}"
        )

    lines = []
    for name, app in sorted(apps.items()):
        cdp = f", cdp={app.get('cdp_url')}" if app.get("cdp_url") else ""
        lines.append(f"{name}: cwd={app['cwd']}, command={app['command']!r}, window={app['window_match']!r}{cdp}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OBS connection and setup
# ---------------------------------------------------------------------------
def _get_obs():
    global _obs
    if _obs is None:
        import obsws_python as obs

        host = os.environ.get("OBS_WEBSOCKET_HOST", "localhost")
        port = int(os.environ.get("OBS_WEBSOCKET_PORT", "4455"))
        password = os.environ.get("OBS_WEBSOCKET_PASSWORD", "")
        _obs = obs.ReqClient(host=host, port=port, password=password)
    return _obs


def _open_obs() -> None:
    if sys.platform == "darwin" and os.environ.get("VIDEO_FEEDER_AUTO_OPEN_OBS", "1") != "0":
        subprocess.run(
            ["open", "-a", "OBS"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _wait_for_obs(timeout_secs: float = 20.0):
    _open_obs()
    deadline = _now_epoch() + timeout_secs
    last_error: Exception | None = None
    cl = None
    while _now_epoch() < deadline:
        if cl is None:
            try:
                cl = _get_obs()
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
                continue
        try:
            cl.get_version()
            return cl
        except Exception as exc:
            if "207" in str(exc):
                time.sleep(0.5)
                continue
            last_error = exc
            time.sleep(0.5)
    if last_error:
        raise last_error
    raise RuntimeError("OBS did not become available")


def _obs_try(cl: Any, method: str, *args: Any, warnings: list[str] | None = None, **kwargs: Any) -> Any:
    fn = getattr(cl, method, None)
    if fn is None:
        if warnings is not None:
            warnings.append(f"OBS WebSocket method unavailable: {method}")
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"OBS {method} failed: {exc}")
        return None


def _obs_attr(response: Any, *attrs: str) -> Any:
    for attr in attrs:
        value = getattr(response, attr, None)
        if value is not None:
            return value
    return None


def _response_items(response: Any, *attrs: str) -> list[Any]:
    for attr in attrs:
        value = getattr(response, attr, None)
        if isinstance(value, list):
            return value
    return []


def _verify_mic(cl: Any, warnings: list[str]) -> None:
    response = _obs_try(cl, "get_input_list", warnings=warnings)
    if response is None:
        return
    inputs = _response_items(response, "inputs", "input_list")
    found_audio = False
    found_unmuted = False
    for item in inputs:
        if isinstance(item, dict):
            name = str(item.get("inputName") or item.get("input_name") or item.get("name") or "")
            kind = str(item.get("inputKind") or item.get("input_kind") or item.get("kind") or "")
        else:
            name = str(getattr(item, "input_name", "") or getattr(item, "inputName", ""))
            kind = str(getattr(item, "input_kind", "") or getattr(item, "inputKind", ""))
        if "mic" in name.lower() or "audio" in kind.lower():
            found_audio = True
            mute_response = _obs_try(cl, "get_input_mute", name, warnings=None)
            muted = getattr(mute_response, "input_muted", getattr(mute_response, "inputMuted", None))
            if muted is False or muted is None:
                found_unmuted = True
    if not found_audio:
        warnings.append("No obvious OBS mic/audio input found; narration may not be captured")
    elif not found_unmuted:
        warnings.append("OBS audio inputs appear muted; narration may not be captured")


def _current_obs_state(cl: Any) -> dict[str, Any]:
    profile = _obs_try(cl, "get_current_profile", warnings=None)
    scene_collection = _obs_try(cl, "get_current_scene_collection", warnings=None)
    scene = _obs_try(cl, "get_current_program_scene", warnings=None)
    return {
        "profile": _obs_attr(profile, "current_profile_name", "currentProfileName"),
        "scene_collection": _obs_attr(
            scene_collection,
            "current_scene_collection_name",
            "currentSceneCollectionName",
        ),
        "program_scene": _obs_attr(scene, "current_program_scene_name", "currentProgramSceneName"),
    }


def _restore_obs_state(cl: Any, state: dict[str, Any] | None) -> None:
    if not state:
        return
    if state.get("profile"):
        _obs_try(cl, "set_current_profile", state["profile"], warnings=None)
    if state.get("scene_collection"):
        _obs_try(cl, "set_current_scene_collection", state["scene_collection"], warnings=None)
    if state.get("program_scene"):
        _obs_try(cl, "set_current_program_scene", state["program_scene"], warnings=None)


def _update_existing_source(cl: Any, window_id: int, warnings: list[str]) -> bool:
    fn = getattr(cl, "set_input_settings", None)
    if fn is None:
        warnings.append("OBS WebSocket method unavailable: set_input_settings")
        return False
    settings = {"type": 1, "window": window_id, "show_cursor": True}
    attempts = (
        lambda: fn(QA_SOURCE, settings, True),
        lambda: fn(input_name=QA_SOURCE, input_settings=settings, overlay=True),
    )
    for attempt in attempts:
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception as exc:
            warnings.append(f"OBS set_input_settings failed: {exc}")
            return False
    warnings.append("OBS set_input_settings: signature mismatch")
    return False


def _create_capture_source(cl: Any, window_id: int, warnings: list[str]) -> bool:
    fn = getattr(cl, "create_input", None)
    if fn is None:
        warnings.append("OBS WebSocket method unavailable: create_input")
        return False

    settings = {"type": 1, "window": window_id, "show_cursor": True}
    attempts = (
        lambda: fn(
            scene_name=QA_SCENE,
            input_name=QA_SOURCE,
            input_kind="screen_capture",
            input_settings=settings,
            scene_item_enabled=True,
        ),
        lambda: fn(
            sceneName=QA_SCENE,
            inputName=QA_SOURCE,
            inputKind="screen_capture",
            inputSettings=settings,
            sceneItemEnabled=True,
        ),
        lambda: fn(QA_SCENE, QA_SOURCE, "screen_capture", settings, True),
    )
    errors: list[str] = []
    for attempt in attempts:
        try:
            attempt()
            return True
        except TypeError as exc:
            errors.append(str(exc))
        except Exception as exc:
            if "601" in str(exc) or "already exists" in str(exc).lower():
                _append_warning_dedup(warnings, f"OBS source {QA_SOURCE!r} already exists, updating settings")
                return _update_existing_source(cl, window_id, warnings)
            warnings.append(f"OBS create_input failed: {exc}")
            return False
    warnings.append(f"OBS create_input signature mismatch: {' | '.join(errors)}")
    return False


def _append_warning_dedup(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _has_capture_source_failure(warnings: list[str]) -> bool:
    return any(
        "capture source" in warning
        or "create_input" in warning
        for warning in warnings
    )


def _ensure_obs_capture(
    cl: Any,
    window_id: int,
    record_dir: Path | None = None,
    *,
    manage_profile: bool = True,
) -> list[str]:
    warnings: list[str] = []

    if manage_profile:
        _obs_try(cl, "create_profile", QA_PROFILE, warnings=None)
        _obs_try(cl, "set_current_profile", QA_PROFILE, warnings=warnings)
        _obs_try(cl, "create_scene_collection", QA_SCENE_COLLECTION, warnings=None)
        _obs_try(cl, "set_current_scene_collection", QA_SCENE_COLLECTION, warnings=warnings)

    if record_dir is not None:
        record_dir.mkdir(parents=True, exist_ok=True)
        _obs_try(cl, "set_record_directory", str(record_dir), warnings=warnings)

    _obs_try(cl, "create_scene", QA_SCENE, warnings=None)
    _obs_try(cl, "set_current_program_scene", QA_SCENE, warnings=warnings)
    _obs_try(cl, "remove_input", QA_SOURCE, warnings=warnings)
    created = _create_capture_source(cl, window_id, warnings)
    if not created:
        warnings.append(f"Could not create OBS window capture source for window {window_id}")

    _verify_mic(cl, warnings)
    return warnings


# ---------------------------------------------------------------------------
# Window detection
# ---------------------------------------------------------------------------
def _list_window_dicts() -> list[dict[str, Any]]:
    try:
        import Quartz
    except ImportError as exc:
        raise RuntimeError("pyobjc-framework-Quartz not installed (macOS window detection only)") from exc

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    skip = {"WindowManager", "Control Center", "Window Server", "Dock", "Notification Center"}
    result = []
    for window in windows:
        owner = window.get("kCGWindowOwnerName", "")
        title = window.get("kCGWindowName", "")
        window_id = window.get("kCGWindowNumber", 0)
        if title and owner not in skip:
            result.append({"id": window_id, "owner": owner, "title": title})
    return result


def _find_window(window_match: str) -> dict[str, Any] | None:
    needle = window_match.lower()
    title_match = None
    for window in _list_window_dicts():
        if needle in window["owner"].lower():
            return window
        if title_match is None and needle in window["title"].lower():
            title_match = window
    return title_match


def _wait_for_window(window_match: str, timeout_secs: float = 30.0) -> dict[str, Any] | None:
    deadline = _now_epoch() + timeout_secs
    while _now_epoch() < deadline:
        window = _find_window(window_match)
        if window:
            return window
        time.sleep(0.5)
    return _find_window(window_match)


@mcp.tool()
def list_windows() -> str:
    """List all capturable windows on screen."""
    try:
        windows = _list_window_dicts()
    except Exception as exc:
        return f"Error: {exc}"

    if not windows:
        return "No capturable windows found."
    return "\n".join(f"{w['id']} | {w['owner']} | {w['title']}" for w in windows)


# ---------------------------------------------------------------------------
# App launch and log scoping
# ---------------------------------------------------------------------------
def _start_app_process(app: dict[str, Any], run_dir: Path) -> tuple[subprocess.Popen[Any], Path]:
    cwd = _expand_path(str(app["cwd"]))
    command = app["command"]
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    full_log = logs_dir / "app.full.log"

    if isinstance(command, list):
        popen_command = [str(part) for part in command]
        shell = False
    else:
        popen_command = str(command)
        shell = True

    process = subprocess.Popen(
        popen_command,
        cwd=str(cwd),
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )

    def pump() -> None:
        assert process.stdout is not None
        with full_log.open("a", encoding="utf-8", errors="replace") as handle:
            for line in process.stdout:
                handle.write(line)
                handle.flush()

    thread = threading.Thread(target=pump, name=f"vf-log-{run_dir.name}", daemon=True)
    thread.start()
    return process, full_log


def _configured_log_paths(app: dict[str, Any], app_full_log: Path) -> list[dict[str, str]]:
    cwd = _expand_path(str(app["cwd"]))
    entries = [{"label": "app.full.log", "path": str(app_full_log)}]
    seen_labels: set[str] = {"app.full.log"}
    for raw_path in app.get("extra_logs", []) or []:
        path = _expand_path(str(raw_path), cwd)
        label = path.name
        if label in seen_labels:
            label = f"{path.parent.name}/{label}"
        seen_labels.add(label)
        entries.append({"label": label, "path": str(path)})
    return entries


def _capture_log_offsets(logs: list[dict[str, str]]) -> dict[str, int]:
    offsets: dict[str, int] = {}
    for entry in logs:
        path = Path(entry["path"])
        offsets[entry["label"]] = path.stat().st_size if path.exists() else 0
    return offsets


def _slice_file_by_offset(source: Path, dest: Path, offset: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        dest.write_text(f"[missing log file: {source}]\n", encoding="utf-8")
        return 0
    size = source.stat().st_size
    safe_offset = 0 if offset > size else max(offset, 0)
    with source.open("rb") as src, dest.open("wb") as dst:
        src.seek(safe_offset)
        shutil.copyfileobj(src, dst)
    return size - safe_offset


def _parse_log_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Treat very large numeric timestamps as milliseconds.
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        numeric = float(text)
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _write_timestamp_filtered_jsonl(source: Path, dest: Path, start_epoch: float, stop_epoch: float) -> int:
    timestamp_keys = ("ts", "timestamp", "time", "datetime", "date")
    count = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return 0
    with source.open("r", encoding="utf-8", errors="replace") as src, dest.open(
        "w", encoding="utf-8"
    ) as dst:
        for line in src:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_ts = None
            for key in timestamp_keys:
                event_ts = _parse_log_timestamp(payload.get(key))
                if event_ts is not None:
                    break
            if event_ts is not None and start_epoch <= event_ts <= stop_epoch:
                dst.write(json.dumps(payload, sort_keys=True) + "\n")
                count += 1
    if count == 0:
        dest.unlink(missing_ok=True)
    return count


def _slice_capture_logs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    run_dir = _run_dir(manifest["run_id"])
    start_epoch = float(manifest["capture_start_epoch"])
    stop_epoch = float(manifest["capture_stop_epoch"])
    offsets = manifest.get("log_offsets", {})
    scoped: list[dict[str, Any]] = []

    for entry in manifest.get("logs", []):
        label = entry["label"]
        source = Path(entry["path"])
        safe_name = label.replace("/", "_").replace(":", "_")
        scoped_path = run_dir / "logs" / f"{safe_name}.capture.log"
        bytes_written = _slice_file_by_offset(source, scoped_path, int(offsets.get(label, 0)))
        scoped_entry: dict[str, Any] = {
            "label": label,
            "source": str(source),
            "path": str(scoped_path),
            "bytes": bytes_written,
            "mode": "byte_offset",
        }

        filtered_path = run_dir / "logs" / f"{safe_name}.timestamp-filtered.ndjson"
        filtered_count = _write_timestamp_filtered_jsonl(scoped_path, filtered_path, start_epoch, stop_epoch)
        if filtered_count:
            scoped_entry["timestamp_filtered_path"] = str(filtered_path)
            scoped_entry["timestamp_filtered_events"] = filtered_count
        scoped.append(scoped_entry)

    manifest["scoped_logs"] = scoped
    return scoped


# ---------------------------------------------------------------------------
# CDP recording
# ---------------------------------------------------------------------------
def _url_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _resolve_cdp_ws_url(cdp_url: str) -> tuple[str, dict[str, Any]]:
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url, {"source": "direct"}

    base = cdp_url.rstrip("/")
    list_url = urllib.parse.urljoin(base + "/", "json/list")
    version_url = urllib.parse.urljoin(base + "/", "json/version")
    try:
        targets = _url_json(list_url)
        if isinstance(targets, list):
            for target in targets:
                if target.get("webSocketDebuggerUrl") and target.get("type") == "page":
                    return target["webSocketDebuggerUrl"], target
            for target in targets:
                if target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"], target
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass

    version = _url_json(version_url)
    if not version.get("webSocketDebuggerUrl"):
        raise RuntimeError(f"No webSocketDebuggerUrl found at {cdp_url}")
    return version["webSocketDebuggerUrl"], version


class CdpRecorder:
    def __init__(self, cdp_url: str, output_path: Path) -> None:
        self.cdp_url = cdp_url
        self.output_path = output_path
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.ws: Any = None
        self._next_id = 1

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._run, name=f"vf-cdp-{self.output_path.parent.name}", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self.stop_event.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def _emit(self, handle, kind: str, payload: dict[str, Any]) -> None:
        handle.write(
            json.dumps(
                {
                    "ts": _iso(),
                    "ts_epoch": _now_epoch(),
                    "source": "cdp",
                    "kind": kind,
                    "payload": payload,
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()

    def _send(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.ws is None:
            return
        message = {"id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._next_id += 1
        self.ws.send(json.dumps(message))

    def _interesting_event(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        params = message.get("params", {})
        if method in {
            "Runtime.exceptionThrown",
            "Runtime.consoleAPICalled",
            "Console.messageAdded",
            "Log.entryAdded",
            "Network.loadingFailed",
            "Page.frameNavigated",
            "Page.domContentEventFired",
            "Page.loadEventFired",
        }:
            return {"method": method, "params": params}
        if method == "Network.responseReceived":
            status = params.get("response", {}).get("status")
            if isinstance(status, (int, float)) and status >= 400:
                return {"method": method, "params": params}
        return None

    def _run(self) -> None:
        try:
            import websocket
        except Exception as exc:
            with self.output_path.open("a", encoding="utf-8") as handle:
                self._emit(handle, "error", {"message": f"websocket-client unavailable: {exc}"})
            return

        with self.output_path.open("a", encoding="utf-8") as handle:
            try:
                ws_url, target = _resolve_cdp_ws_url(self.cdp_url)
                self._emit(handle, "target", {"cdp_url": self.cdp_url, "ws_url": ws_url, "target": target})
                self.ws = websocket.create_connection(ws_url, timeout=5)
                self.ws.settimeout(0.5)
                for method in ("Runtime.enable", "Console.enable", "Log.enable", "Network.enable", "Page.enable"):
                    self._send(method)
            except Exception as exc:
                self._emit(handle, "error", {"message": f"CDP connection failed: {exc}"})
                return

            while not self.stop_event.is_set():
                try:
                    raw = self.ws.recv()
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    if exc.__class__.__name__ in {"WebSocketTimeoutException", "TimeoutError"}:
                        continue
                    self._emit(handle, "error", {"message": f"CDP receive failed: {exc}"})
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = self._interesting_event(message)
                if event is not None:
                    self._emit(handle, "event", event)


def _start_cdp_if_configured(manifest: dict[str, Any]) -> None:
    cdp_url = manifest.get("app", {}).get("cdp_url")
    if not cdp_url:
        return
    recorder = CdpRecorder(str(cdp_url), _run_dir(manifest["run_id"]) / "cdp-events.ndjson")
    recorder.start()
    with _state_lock:
        _active_sessions.setdefault(manifest["run_id"], {})["cdp"] = recorder
    manifest["cdp_events_path"] = str(recorder.output_path)


def _stop_cdp(run_id: str) -> None:
    with _state_lock:
        recorder = _active_sessions.get(run_id, {}).get("cdp")
    if recorder is not None:
        recorder.stop()


# ---------------------------------------------------------------------------
# Context and Gemini analysis
# ---------------------------------------------------------------------------
def _repo_context(cwd: str | Path) -> str:
    repo = _expand_path(str(cwd))

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(repo),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            return ""
        return result.stdout.strip()

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return f"Working directory: {repo}\nGit: not a repository or unavailable\n"

    branch = git("branch", "--show-current") or "(detached)"
    head = git("rev-parse", "HEAD")
    commits = git("log", "--oneline", "-5")
    status = git("status", "--short")
    return (
        f"Working directory: {repo}\n"
        f"Git branch: {branch}\n"
        f"Git HEAD: {head}\n"
        f"Recent commits:\n{commits or '[none]'}\n"
        f"Working tree status:\n{status or '[clean]'}\n"
    )


def _write_context(manifest: dict[str, Any], user_context: str = "") -> Path:
    run_dir = _run_dir(manifest["run_id"])
    path = run_dir / "context.md"
    repo_text = _repo_context(manifest["app"]["cwd"])
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    sections = []
    if existing:
        sections.append(existing.rstrip())
    if user_context:
        sections.append(f"## User Context\n\n{user_context.strip()}")
    sections.append(f"## Repository Context\n\n```text\n{repo_text.rstrip()}\n```")
    path.write_text("\n\n".join(sections).rstrip() + "\n", encoding="utf-8")
    manifest["context_path"] = str(path)
    return path


def _get_gemini():
    if genai is None:
        return None, "google-genai not installed"
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not set"
    return genai.Client(api_key=api_key), None


def _build_evidence_prompt(manifest: dict[str, Any]) -> str:
    run_dir = _run_dir(manifest["run_id"])
    budget = PROMPT_TEXT_BUDGET
    parts = [
        WITNESS_PROMPT,
        "\n\n## QA Session Manifest\n",
        _safe_json_text(manifest, min(12000, budget // 3)),
    ]

    context_path = Path(manifest.get("context_path", run_dir / "context.md"))
    if context_path.exists():
        parts.extend(["\n\n## Current Work Context\n", _read_text_excerpt(context_path, min(16000, budget // 3))])

    for scoped in manifest.get("scoped_logs", [])[:6]:
        log_path = Path(scoped["path"])
        parts.extend(
            [
                f"\n\n## Scoped Log: {scoped['label']}\n",
                "```text\n",
                _read_text_excerpt(log_path, min(12000, budget // 4)),
                "\n```",
            ]
        )
        if scoped.get("timestamp_filtered_path"):
            filtered = Path(scoped["timestamp_filtered_path"])
            parts.extend(
                [
                    f"\n\n## Timestamp Filtered JSONL: {scoped['label']}\n",
                    "```jsonl\n",
                    _read_text_excerpt(filtered, min(8000, budget // 6)),
                    "\n```",
                ]
            )

    cdp_value = manifest.get("cdp_events_path")
    cdp_path = Path(cdp_value) if cdp_value else None
    if cdp_path and cdp_path.is_file():
        parts.extend(
            [
                "\n\n## CDP Events\n",
                "```jsonl\n",
                _read_text_excerpt(cdp_path, min(16000, budget // 3)),
                "\n```",
            ]
        )

    return "".join(parts)


def _upload_video(path: Path) -> tuple[dict[str, str] | None, str]:
    """Upload a video to Gemini and return (file_ref, error).

    file_ref has keys: name, uri, mime_type.  On error, file_ref is None and
    the second element is the error message.
    """
    client, err = _get_gemini()
    if err:
        return None, f"Gemini error: {err}"

    try:
        video_file = client.files.upload(file=path)
    except Exception as exc:
        return None, f"Gemini error: video upload failed: {exc}"

    deadline = _now_epoch() + GEMINI_PROCESSING_TIMEOUT_SECONDS
    while video_file.state == "PROCESSING":
        if _now_epoch() >= deadline:
            return None, f"Gemini error: video processing timed out after {GEMINI_PROCESSING_TIMEOUT_SECONDS}s"
        time.sleep(2)
        try:
            video_file = client.files.get(name=video_file.name)
        except Exception as exc:
            return None, f"Gemini error: video processing poll failed: {exc}"

    if video_file.state != "ACTIVE":
        return None, f"Gemini error: video processing failed (state: {video_file.state})"

    file_ref = {
        "name": video_file.name,
        "uri": video_file.uri,
        "mime_type": video_file.mime_type,
    }
    return file_ref, ""


def _query_gemini_with_ref(file_ref: dict[str, str], prompt: str) -> str:
    """Send a prompt to Gemini referencing an already-uploaded file."""
    client, err = _get_gemini()
    if err:
        return f"Gemini error: {err}"

    content_part = types.Part.from_uri(
        file_uri=file_ref["uri"],
        mime_type=file_ref["mime_type"],
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[content_part, prompt],
        )
    except Exception as exc:
        return f"Gemini error: content generation failed: {exc}"
    if not response.text:
        return "Gemini error: response contained no text"
    return response.text


def _upload_video_and_analyze(path: Path, prompt: str) -> str:
    """Upload + single-shot query. Used by legacy analyze_bug flow."""
    file_ref, err = _upload_video(path)
    if err:
        return err
    return _query_gemini_with_ref(file_ref, prompt)


def _is_analysis_error(analysis: str) -> bool:
    return analysis.startswith("Gemini error:")


def _copy_recording_into_run(output_path: str | Path, run_dir: Path) -> Path:
    source = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Recording file not found at {source}")
    suffix = source.suffix or ".mp4"
    dest = run_dir / f"recording{suffix}"
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    return dest


def _stop_obs_recording(manifest: dict[str, Any]) -> str:
    if manifest.get("obs_output_path"):
        return str(manifest["obs_output_path"])
    cl = _get_obs()
    result = cl.stop_record()
    output_path = result.output_path
    manifest["obs_output_path"] = output_path
    return output_path


def _auto_stop_capture(run_id: str, max_seconds: int) -> None:
    time.sleep(max_seconds)
    with _lock_for_run(run_id):
        try:
            manifest = _load_manifest(run_id)
        except Exception:
            return
        if manifest.get("status") != "recording":
            return
        stop_epoch = _now_epoch()
        manifest["status"] = "stopping"
        _set_active_status(run_id, "stopping")
        _save_manifest(manifest)
        try:
            output_path = _stop_obs_recording(manifest)
            manifest["status"] = "auto_stopped"
            _set_active_status(run_id, "auto_stopped")
            manifest["capture_stop_epoch"] = stop_epoch
            manifest["capture_stop_iso"] = _iso(stop_epoch)
            manifest["obs_output_path"] = output_path
            _append_warning(manifest, f"Capture auto-stopped after {max_seconds} seconds")
            _stop_cdp(run_id)
            _save_manifest(manifest)
        except Exception as exc:
            manifest["status"] = "stop_failed"
            _set_active_status(run_id, "stop_failed")
            _append_warning(manifest, f"Auto-stop failed: {exc}")
            _save_manifest(manifest)


# ---------------------------------------------------------------------------
# QA session MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def prepare_session(app_name: str, context: str = "") -> str:
    """Launch a configured app, prepare OBS capture, and create a QA run."""
    try:
        apps = _load_apps_config()
        if app_name not in apps:
            return f"Error: app {app_name!r} is not configured. Run list_apps() to see available apps."
        app = dict(apps[app_name])
        app["cwd"] = str(_expand_path(str(app["cwd"])))

        run_id = _new_run_id()
        run_dir = _run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        process, full_log = _start_app_process(app, run_dir)
        logs = _configured_log_paths(app, full_log)
        manifest: dict[str, Any] = {
            "run_id": run_id,
            "status": "prepared",
            "created_epoch": _now_epoch(),
            "created_iso": _iso(),
            "run_dir": str(run_dir),
            "app_name": app_name,
            "app": app,
            "logs": logs,
            "app_process_pid": process.pid,
            "warnings": [],
        }
        _write_context(manifest, context)

        with _state_lock:
            _active_sessions[run_id] = {
                "process": process,
                "app_full_log": full_log,
                "status": "prepared",
            }

        window = _wait_for_window(str(app["window_match"]), float(app.get("window_timeout_secs", 30)))
        if window:
            manifest["window"] = window
            try:
                cl = _wait_for_obs()
                manifest.setdefault("obs_previous_state", _current_obs_state(cl))
                warnings = _ensure_obs_capture(cl, int(window["id"]), run_dir)
                for warning in warnings:
                    _append_warning(manifest, warning)
            except Exception as exc:
                _append_warning(manifest, f"OBS preparation failed: {exc}")
        else:
            _append_warning(manifest, f"No window matched {app['window_match']!r}; start_capture will retry")

        _save_manifest(manifest)
        warning_text = "\n".join(f"- {w}" for w in manifest.get("warnings", []))
        return (
            f"Prepared Video Feeder run {run_id}\n"
            f"Run dir: {run_dir}\n"
            f"App PID: {process.pid}\n"
            f"Next: start_capture(run_id={run_id!r})"
            + (f"\nWarnings:\n{warning_text}" if warning_text else "")
        )
    except Exception as exc:
        return f"Error preparing session: {exc}"


@mcp.tool()
def start_capture(run_id: str = "", max_seconds: int = DEFAULT_CAPTURE_MAX_SECONDS) -> str:
    """Start OBS recording and mark the evidence start boundary for a QA run."""
    if max_seconds < 1:
        return f"Error: max_seconds must be positive, got {max_seconds}"
    try:
        resolved_run_id = _resolve_run_id(run_id)
        with _lock_for_run(resolved_run_id):
            manifest = _load_manifest(resolved_run_id)
            status = manifest.get("status")
            if status == "recording":
                return f"Run {resolved_run_id} is already recording."
            if status != "prepared":
                return f"Error: run {resolved_run_id} is {status!r}; only prepared runs can start capture."

            app = manifest["app"]
            window = manifest.get("window") or _wait_for_window(str(app["window_match"]), 10)
            if not window:
                return f"Error: no window matched {app['window_match']!r}"
            manifest["window"] = window

            cl = _wait_for_obs()
            manifest.setdefault("obs_previous_state", _current_obs_state(cl))
            warnings = _ensure_obs_capture(cl, int(window["id"]), _run_dir(resolved_run_id))
            for warning in warnings:
                _append_warning(manifest, warning)
            if _has_capture_source_failure(warnings):
                _save_manifest(manifest)
                return f"Error: OBS capture source was not created for run {resolved_run_id}."

            start_epoch = _now_epoch()
            manifest["capture_start_epoch"] = start_epoch
            manifest["capture_start_iso"] = _iso(start_epoch)
            manifest["log_offsets"] = _capture_log_offsets(manifest.get("logs", []))
            manifest["max_capture_seconds"] = max_seconds
            manifest["status"] = "starting"
            _set_active_status(resolved_run_id, "starting")
            _save_manifest(manifest)
            try:
                cl.start_record()
            except Exception as exc:
                manifest["status"] = "prepared"
                _set_active_status(resolved_run_id, "prepared")
                _append_warning(manifest, f"OBS start_record failed: {exc}")
                _stop_cdp(resolved_run_id)
                _save_manifest(manifest)
                return f"Error starting OBS recording for run {resolved_run_id}: {exc}"

            manifest["status"] = "recording"
            _set_active_status(resolved_run_id, "recording")
            _start_cdp_if_configured(manifest)
            _save_manifest(manifest)

            timer = threading.Thread(
                target=_auto_stop_capture,
                args=(resolved_run_id, max_seconds),
                name=f"vf-autostop-{resolved_run_id}",
                daemon=True,
            )
            timer.start()
            with _state_lock:
                _active_sessions.setdefault(resolved_run_id, {})["auto_stop_timer"] = timer

            return (
                f"Recording started for run {resolved_run_id}.\n"
                f"Capture start: {manifest['capture_start_iso']}\n"
                f"Run dir: {_run_dir(resolved_run_id)}\n"
                f"Max duration: {max_seconds}s"
            )
    except Exception as exc:
        return f"Error starting capture: {exc}"


@mcp.tool()
def stop_and_analyze(run_id: str = "", context: str = "") -> str:
    """Stop a QA run and analyze it, or stop a legacy raw OBS recording if no run exists."""
    if run_id:
        try:
            resolved_run_id = _validate_run_id(run_id)
        except ValueError:
            if context:
                return f"Error: invalid run_id {run_id!r}"
            return _stop_raw_and_analyze(context=run_id)
        if not _manifest_path(resolved_run_id).exists():
            if context or "-" in run_id or _looks_like_generated_run_id(run_id):
                return f"Error: run {run_id!r} was not found"
            return _stop_raw_and_analyze(context=run_id)
    else:
        try:
            resolved_run_id = _resolve_run_id("")
        except FileNotFoundError:
            return _stop_raw_and_analyze(context=context)

    with _lock_for_run(resolved_run_id):
        manifest = _load_manifest(resolved_run_id)
        status = manifest.get("status")
        if status in {"prepared", "starting"}:
            return f"Error: run {resolved_run_id} is {status!r}; no recording has started."
        if status in {"analyzed", "reported"}:
            report_path = manifest.get("witness_report_path") or manifest.get("analysis_path", "(unknown)")
            return (
                f"Run {resolved_run_id} already has a witness report.\n"
                f"Report: {report_path}\n"
                f"Use ask_witness(run_id={resolved_run_id!r}, question='...') for follow-ups."
            )
        if status == "stopping":
            return f"Run {resolved_run_id} is already stopping; wait and retry with the run_id."
        if status not in {"recording", "auto_stopped", "stopped", "analysis_failed", "stop_failed"}:
            return f"Error: run {resolved_run_id} is in unsupported status {status!r}."

        if context:
            _write_context(manifest, context)

        output_path = manifest.get("obs_output_path")
        should_stop_obs = status in {"recording", "stop_failed"}
        if status == "auto_stopped" and output_path:
            pass
        elif status in {"stopped", "analysis_failed"} and (
            manifest.get("recording_path") or output_path
        ):
            pass
        elif should_stop_obs:
            manifest["status"] = "stopping"
            _set_active_status(resolved_run_id, "stopping")
            _save_manifest(manifest)
            try:
                output_path = _stop_obs_recording(manifest)
            except Exception as exc:
                manifest["status"] = "stop_failed"
                _set_active_status(resolved_run_id, "stop_failed")
                _append_warning(manifest, f"OBS stop_record failed: {exc}")
                _save_manifest(manifest)
                return f"Error stopping recording for run {resolved_run_id}: {exc}"
        else:
            return f"Error: run {resolved_run_id} has no recording to analyze."

        stop_epoch = manifest.get("capture_stop_epoch") or _now_epoch()
        manifest["capture_stop_epoch"] = stop_epoch
        manifest["capture_stop_iso"] = _iso(float(stop_epoch))
        _stop_cdp(resolved_run_id)
        manifest["status"] = "stopped"
        _set_active_status(resolved_run_id, "stopped")
        _save_manifest(manifest)

        try:
            cl = _get_obs()
            _obs_try(cl, "remove_input", QA_SOURCE, warnings=None)
            _restore_obs_state(cl, manifest.get("obs_previous_state"))
        except Exception:
            pass

        try:
            if manifest.get("recording_path"):
                recording_path = Path(manifest["recording_path"])
            else:
                recording_path = _copy_recording_into_run(output_path, _run_dir(resolved_run_id))
            manifest["recording_path"] = str(recording_path)
            if not manifest.get("scoped_logs"):
                _slice_capture_logs(manifest)
            _save_manifest(manifest)
        except Exception as exc:
            manifest["status"] = "analysis_failed"
            _set_active_status(resolved_run_id, "analysis_failed")
            _append_warning(manifest, f"Evidence packaging failed: {exc}")
            _release_active_session(manifest)
            _save_manifest(manifest)
            return (
                f"Run {resolved_run_id} captured, but evidence packaging failed.\n"
                f"Run dir: {_run_dir(resolved_run_id)}\n"
                f"Recording source: {output_path or '(unknown)'}\n\n"
                f"Error: {exc}"
            )

        file_ref = manifest.get("gemini_file_ref")
        if not file_ref:
            try:
                file_ref, upload_err = _upload_video(recording_path)
            except Exception as exc:
                upload_err = f"Gemini error: upload failed: {exc}"
                file_ref = None
            if upload_err:
                analysis_path = _run_dir(resolved_run_id) / "witness-report.md"
                analysis_path.write_text(f"# Witness Report\n\n{upload_err}\n", encoding="utf-8")
                manifest["analysis_path"] = str(analysis_path)
                manifest["status"] = "analysis_failed"
                _set_active_status(resolved_run_id, "analysis_failed")
                _release_active_session(manifest)
                _save_manifest(manifest)
                return (
                    f"Run {resolved_run_id} captured, but upload failed.\n"
                    f"Run dir: {_run_dir(resolved_run_id)}\n"
                    f"Recording: {recording_path}\n\n"
                    f"{upload_err}"
                )
            manifest["gemini_file_ref"] = file_ref
            _save_manifest(manifest)

        prompt = _build_evidence_prompt(manifest)
        try:
            witness_report = _query_gemini_with_ref(file_ref, prompt)
        except Exception as exc:
            witness_report = f"Gemini error: witness query failed: {exc}"
        analysis_path = _run_dir(resolved_run_id) / "witness-report.md"
        analysis_path.write_text(f"# Witness Report\n\n{witness_report}\n", encoding="utf-8")
        manifest["analysis_path"] = str(analysis_path)
        manifest["witness_report_path"] = str(analysis_path)
        if _is_analysis_error(witness_report):
            manifest["status"] = "analysis_failed"
            _set_active_status(resolved_run_id, "analysis_failed")
            _release_active_session(manifest)
            _save_manifest(manifest)
            return (
                f"Run {resolved_run_id} captured, but witness query failed.\n"
                f"Run dir: {_run_dir(resolved_run_id)}\n"
                f"Recording: {recording_path}\n"
                f"Report: {analysis_path}\n\n"
                f"{witness_report}"
            )

        manifest["status"] = "reported"
        _set_active_status(resolved_run_id, "reported")
        _release_active_session(manifest)
        _save_manifest(manifest)

        return (
            f"Run {resolved_run_id} — witness report ready.\n"
            f"Run dir: {_run_dir(resolved_run_id)}\n"
            f"Recording: {recording_path}\n"
            f"Report: {analysis_path}\n\n"
            f"{witness_report}\n\n"
            f"Use ask_witness(run_id={resolved_run_id!r}, question='...') to ask follow-up questions."
        )


@mcp.tool()
def ask_witness(question: str, run_id: str = "") -> str:
    """Ask a follow-up question about a recorded QA session.

    Gemini re-examines the same video to answer. Use this to ask about
    specific moments, clarify what was on screen, or get details the
    initial witness report didn't cover. The video is NOT re-uploaded.
    """
    try:
        resolved_run_id = _resolve_run_id(run_id, include_reported=True)
        manifest = _load_manifest(resolved_run_id)
    except (FileNotFoundError, ValueError) as exc:
        return f"Error: {exc}"
    file_ref = manifest.get("gemini_file_ref")
    if not file_ref:
        return (
            f"Error: run {resolved_run_id} has no uploaded video reference. "
            f"Run stop_and_analyze first to generate a witness report."
        )

    report_path = manifest.get("witness_report_path") or manifest.get("analysis_path")
    previous_report = ""
    if report_path:
        p = Path(report_path)
        if p.exists():
            previous_report = p.read_text(encoding="utf-8", errors="replace")

    prompt = FOLLOWUP_PROMPT.format(
        previous_report=previous_report or "(no previous report available)",
        question=question,
    )

    try:
        answer = _query_gemini_with_ref(file_ref, prompt)
    except Exception as exc:
        return f"Error querying witness: {exc}"

    if _is_analysis_error(answer):
        return f"Witness follow-up failed for run {resolved_run_id}.\n\n{answer}"

    run_dir = _run_dir(resolved_run_id)
    followups_dir = run_dir / "followups"
    with _lock_for_run(resolved_run_id):
        followups_dir.mkdir(parents=True, exist_ok=True)
        existing = list(followups_dir.glob("followup-*.md"))
        idx = len(existing) + 1
        followup_path = followups_dir / f"followup-{idx:03d}.md"
        followup_path.write_text(
            f"# Follow-up #{idx}\n\n**Q:** {question}\n\n**A:**\n\n{answer}\n",
            encoding="utf-8",
        )

    return (
        f"Witness follow-up #{idx} for run {resolved_run_id}:\n"
        f"Saved: {followup_path}\n\n"
        f"{answer}"
    )


@mcp.tool()
def qa_prepare(app_name: str, context: str = "") -> str:
    """Slash-command friendly wrapper for /qa prepare <app>."""
    return prepare_session(app_name=app_name, context=context)


@mcp.tool()
def qa_record(run_id: str = "", max_seconds: int = DEFAULT_CAPTURE_MAX_SECONDS) -> str:
    """Slash-command friendly wrapper for /qa record."""
    return start_capture(run_id=run_id, max_seconds=max_seconds)


@mcp.tool()
def qa_stop(run_id: str = "", context: str = "") -> str:
    """Slash-command friendly wrapper for /qa stop."""
    return stop_and_analyze(run_id=run_id, context=context)


@mcp.tool()
def qa_ask(question: str, run_id: str = "") -> str:
    """Slash-command friendly wrapper for /qa ask <question>."""
    return ask_witness(question=question, run_id=run_id)


@mcp.tool()
def qa_analyze(file_path: str, context: str = "") -> str:
    """Slash-command friendly wrapper for /qa analyze <path>."""
    return analyze_bug(file_path=file_path, context=context)


# ---------------------------------------------------------------------------
# Legacy/simple tools
# ---------------------------------------------------------------------------
@mcp.tool()
def start_recording(window_id: int = 0, window_name: str = "") -> str:
    """Start a simple OBS recording of a window without a QA session."""
    if not window_id and window_name:
        try:
            window = _find_window(window_name)
            if window:
                window_id = int(window["id"])
        except Exception as exc:
            return f"Error: {exc}"

    if not window_id:
        return "Error: Could not find window. Provide a valid window_id or window_name."

    try:
        cl = _wait_for_obs()
        warnings = _ensure_obs_capture(cl, int(window_id), None, manage_profile=False)
        if _has_capture_source_failure(warnings):
            return f"Error: OBS capture source was not created for window {window_id}."
        cl.start_record()
        warning_text = "\n".join(f"- {w}" for w in warnings)
        return (
            f"Recording started — capturing window {window_id}. Say 'done' when you've reproduced the bug."
            + (f"\nWarnings:\n{warning_text}" if warning_text else "")
        )
    except Exception as exc:
        return f"Error: {exc}"


def _stop_raw_and_analyze(context: str = "") -> str:
    try:
        cl = _get_obs()
        result = cl.stop_record()
        output_path = result.output_path
    except Exception as exc:
        return f"Error stopping recording: {exc}"

    try:
        cl.remove_input(QA_SOURCE)
    except Exception:
        pass

    path = Path(output_path)
    if not path.exists():
        return f"Error: Recording file not found at {output_path}"

    prompt = BUG_PROMPT
    if context:
        prompt += f"\n\nAdditional context from the developer:\n{context}"

    try:
        analysis = _upload_video_and_analyze(path, prompt)
        if analysis.startswith("Gemini error:"):
            return f"Recording saved to {output_path}, but {analysis}"
        return analysis
    except Exception as exc:
        return f"Recording saved to {output_path}, but Gemini analysis failed: {exc}"


@mcp.tool()
def analyze_bug(file_path: str, context: str = "") -> str:
    """Analyze an existing screen recording or screenshot of a bug."""
    client, err = _get_gemini()
    if err:
        return f"Error: {err}"

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: File not found: {path}"

    suffix = path.suffix.lower()
    is_video = suffix in VIDEO_EXTENSIONS
    is_image = suffix in IMAGE_EXTENSIONS

    if not is_video and not is_image:
        return (
            f"Error: Unsupported file type '{suffix}'. Use video "
            f"({', '.join(sorted(VIDEO_EXTENSIONS))}) or image ({', '.join(sorted(IMAGE_EXTENSIONS))})"
        )

    prompt = BUG_PROMPT
    if context:
        prompt += f"\n\nAdditional context from the developer:\n{context}"

    if is_video:
        try:
            return _upload_video_and_analyze(path, prompt)
        except Exception as exc:
            return f"Gemini analysis failed: {exc}"

    mime = IMAGE_MIMES.get(suffix, "image/png")
    try:
        content_part = types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mime,
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[content_part, prompt],
        )
    except Exception as exc:
        return f"Error: Gemini image analysis failed: {exc}"
    if not response.text:
        return "Error: Gemini returned no text for image analysis"
    return response.text


if __name__ == "__main__":
    mcp.run()
