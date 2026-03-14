# /// script
# dependencies = ["fastmcp", "google-genai"]
# ///
"""Bug video analyzer — drop a screen recording, Gemini explains the bug."""

import os
import time
from pathlib import Path

from google import genai
from google.genai import types
from fastmcp import FastMCP

mcp = FastMCP("bug-video")

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


def _get_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not set"
    return genai.Client(api_key=api_key), None


@mcp.tool()
def analyze_bug(file_path: str, context: str = "") -> str:
    """Analyze a screen recording or screenshot of a bug that's hard to describe.

    Drop a video or image file and get a detailed analysis of what the bug
    appears to be, likely root causes, and follow-up questions.

    Args:
        file_path: Path to the video (.mp4, .webm, .mov) or image (.png, .jpg) file
        context: Optional — what you're working on, what you think the bug might be,
                 what repo/file/feature is involved
    """
    client, err = _get_client()
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
        # Upload to Gemini File API (videos can be large)
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
        # Images can be inlined directly
        mime = IMAGE_MIMES.get(suffix, "image/png")
        content_part = types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mime,
        )

    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[content_part, prompt],
    )

    return response.text


if __name__ == "__main__":
    mcp.run()
