# /// script
# dependencies = [
#   "fastmcp",
#   "google-genai",
#   "obsws-python",
#   "pyobjc-framework-Quartz; sys_platform == 'darwin'",
# ]
# ///
"""Video Feeder — record bugs via OBS and analyze them with Gemini."""

import os
import time
from pathlib import Path

from google import genai
from google.genai import types
from fastmcp import FastMCP

mcp = FastMCP("video-feeder")

# ---------------------------------------------------------------------------
# OBS connection (lazy, so server starts even if OBS isn't running yet)
# ---------------------------------------------------------------------------
_obs = None


def _get_obs():
    global _obs
    if _obs is None:
        import obsws_python as obs

        host = os.environ.get("OBS_WEBSOCKET_HOST", "localhost")
        port = int(os.environ.get("OBS_WEBSOCKET_PORT", "4455"))
        password = os.environ.get("OBS_WEBSOCKET_PASSWORD", "")
        _obs = obs.ReqClient(host=host, port=port, password=password)
    return _obs


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
def _get_gemini():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not set"
    return genai.Client(api_key=api_key), None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

BUG_PROMPT = """\
You are a senior software engineer helping debug an issue that another developer \
recorded because they couldn't describe it in words.

Context: The developer dropped this file into their IDE because the bug is easier \
to show than explain. They might be showing:
- A UI glitch, rendering issue, or visual artifact
- Unexpected behavior when clicking/interacting with something
- A race condition or timing-dependent bug
- Console errors or log output that's hard to summarize
- A sequence of steps that triggers a crash or wrong state
- Performance issues (stuttering, slow loading, hangs)
- A "it works here but not here" comparison
- A test failure or build output they don't understand

Your job:
1. Describe exactly what you see happening, step by step (with timestamps for video)
2. Identify what appears to be the bug or unexpected behavior
3. Note any error messages, console output, or UI state visible
4. Suggest likely root causes based on what you observe
5. Ask 2-3 targeted follow-up questions that would help narrow down the issue

Be concrete and specific. The developer is going to relay your analysis to an AI \
coding assistant, so structure your response to be actionable."""

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

# Scene/source names used by video-feeder
_SCENE = "Video Feeder"
_SOURCE = "VF Capture"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_windows() -> str:
    """List all capturable windows on screen.

    Returns window IDs, application names, and window titles.
    Use the window_name or window_id with start_recording to capture a specific window.
    """
    try:
        import Quartz
    except ImportError:
        return "Error: pyobjc-framework-Quartz not installed (macOS only)"

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    skip = {"WindowManager", "Control Center", "Window Server", "Dock", "Notification Center"}
    lines = []
    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        name = w.get("kCGWindowName", "")
        wid = w.get("kCGWindowNumber", 0)
        if name and owner not in skip:
            lines.append(f"{wid} | {owner} | {name}")

    if not lines:
        return "No capturable windows found."
    return "\n".join(lines)


@mcp.tool()
def start_recording(window_id: int = 0, window_name: str = "") -> str:
    """Start recording a window via OBS.

    Provide either window_id (from list_windows) or window_name (fuzzy match).
    OBS must be running with WebSocket enabled.

    Args:
        window_id: macOS CGWindowID to capture (from list_windows)
        window_name: Window title substring to match (alternative to window_id)
    """
    # Resolve window ID from name if needed
    if not window_id and window_name:
        try:
            import Quartz

            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
            )
            name_lower = window_name.lower()
            for w in windows:
                title = w.get("kCGWindowName", "")
                owner = w.get("kCGWindowOwnerName", "")
                if name_lower in title.lower() or name_lower in owner.lower():
                    window_id = w.get("kCGWindowNumber", 0)
                    break
        except ImportError:
            return "Error: pyobjc-framework-Quartz not installed (macOS only)"

    if not window_id:
        return "Error: Could not find window. Provide a valid window_id or window_name."

    try:
        cl = _get_obs()

        # Create our scene if it doesn't exist
        try:
            cl.create_scene(_SCENE)
        except Exception:
            pass  # already exists

        cl.set_current_program_scene(_SCENE)

        # Remove old capture source if it exists
        try:
            cl.remove_input(_SOURCE)
        except Exception:
            pass

        # Create window capture source targeting the window
        cl.create_input(
            scene_name=_SCENE,
            input_name=_SOURCE,
            input_kind="screen_capture",
            input_settings={
                "type": 1,  # window capture
                "window": window_id,
                "show_cursor": True,
            },
            scene_item_enabled=True,
        )

        # Start recording
        cl.start_record()
        return f"Recording started — capturing window {window_id}. Say 'done' when you've reproduced the bug."

    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stop_and_analyze(context: str = "") -> str:
    """Stop recording and send the video to Gemini for bug analysis.

    Call this after the user has reproduced the bug.

    Args:
        context: What the developer is working on — project, files, framework,
                 what they were trying to do, what they think the bug might be.
                 The richer the context, the better the analysis.
    """
    try:
        cl = _get_obs()
        result = cl.stop_record()
        output_path = result.output_path
    except Exception as e:
        return f"Error stopping recording: {e}"

    # Clean up our capture source
    try:
        cl.remove_input(_SOURCE)
    except Exception:
        pass

    # Send to Gemini
    client, err = _get_gemini()
    if err:
        return f"Recording saved to {output_path}, but Gemini error: {err}"

    path = Path(output_path)
    if not path.exists():
        return f"Error: Recording file not found at {output_path}"

    prompt = BUG_PROMPT
    if context:
        prompt += f"\n\nAdditional context from the developer:\n{context}"

    try:
        video_file = client.files.upload(file=path)

        while video_file.state == "PROCESSING":
            time.sleep(2)
            video_file = client.files.get(name=video_file.name)

        if video_file.state != "ACTIVE":
            return f"Recording saved to {output_path}, but video processing failed (state: {video_file.state})"

        content_part = types.Part.from_uri(
            file_uri=video_file.uri,
            mime_type=video_file.mime_type,
        )

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[content_part, prompt],
        )
        return response.text

    except Exception as e:
        return f"Recording saved to {output_path}, but Gemini analysis failed: {e}"


@mcp.tool()
def analyze_bug(file_path: str, context: str = "") -> str:
    """Analyze an existing screen recording or screenshot of a bug.

    For when you already have a video/image file — no OBS needed.

    Args:
        file_path: Path to the video (.mp4, .webm, .mov) or image (.png, .jpg) file
        context: Optional — what you're working on, what you think the bug might be,
                 what repo/file/feature is involved
    """
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
        return f"Error: Unsupported file type '{suffix}'. Use video ({', '.join(VIDEO_EXTENSIONS)}) or image ({', '.join(IMAGE_EXTENSIONS)})"

    prompt = BUG_PROMPT
    if context:
        prompt += f"\n\nAdditional context from the developer:\n{context}"

    if is_video:
        video_file = client.files.upload(file=path)

        while video_file.state == "PROCESSING":
            time.sleep(2)
            video_file = client.files.get(name=video_file.name)

        if video_file.state != "ACTIVE":
            return f"Error: Video processing failed (state: {video_file.state})"

        content_part = types.Part.from_uri(
            file_uri=video_file.uri,
            mime_type=video_file.mime_type,
        )
    else:
        mime = IMAGE_MIMES.get(suffix, "image/png")
        content_part = types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mime,
        )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[content_part, prompt],
    )

    return response.text


if __name__ == "__main__":
    mcp.run()
