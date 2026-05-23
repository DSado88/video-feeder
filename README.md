# Video Feeder

An MCP server that lets Claude record your screen while you demo a bug, then get an AI witness report it can interrogate.

You know that bug you can't describe? Record it, narrate it, and let the coding agent see what you see.

## Why not just upload to Gemini?

Uploading a video to Gemini on the web gives you one-shot analysis. Video Feeder gives you a **witness loop**:

- **Gemini** has eyes and ears (video + audio). Claude doesn't.
- **Claude** has the codebase. Gemini doesn't.
- Gemini reports facts. Claude traces them to code.

After the initial witness report, Claude can cross-examine: *"was the toolbar visible when the user scrolled at 0:18?"* — Gemini re-watches the same video to answer, Claude maps that to a specific function in the codebase. Multiple rounds, each informed by code-level context.

## How it works

1. You tell Claude **"record orchid"**
2. Claude calls `prepare_session("orchid")` — launches the app, opens OBS, sets up window capture + mic
3. Claude calls `start_capture()` — OBS starts recording
4. You use the app and narrate the bug out loud
5. Claude calls `stop_and_analyze()` — stops recording, slices app logs to just the capture window, sends video + logs to Gemini
6. Gemini writes a **witness report** — what it saw, what it heard, timestamps, log correlations. It does NOT guess at code causes.
7. Claude reads the report. It can call `ask_witness("what was in the network tab at 0:18?")` — Gemini re-watches to answer. Multiple rounds.

### The log trick

When recording starts, Video Feeder snapshots the byte offset of every app log file. When you stop, it slices just the lines that happened during your recording. Gemini gets the video + only the relevant logs — no noise from hours of prior output.

## Tools

| Tool | Step | What it does |
|------|------|-------------|
| `list_apps` | — | Shows configured apps |
| `prepare_session` / `qa_prepare` | 1 | Launches app, opens OBS, sets up capture + mic |
| `start_capture` / `qa_record` | 2 | Starts OBS recording, marks log boundary |
| `stop_and_analyze` / `qa_stop` | 3 | Stops recording, slices logs, sends to Gemini, returns witness report |
| `ask_witness` / `qa_ask` | 4 | Follow-up questions — Gemini re-watches the same video |
| `analyze_bug` / `qa_analyze` | — | Analyze an existing video/screenshot (no OBS needed) |
| `list_windows` | — | Shows capturable windows with IDs (macOS) |
| `start_recording` | — | Legacy: raw OBS recording without session management |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package runner)
- [OBS Studio](https://obsproject.com/) with WebSocket server enabled
- A [Gemini API key](https://aistudio.google.com/apikey)
- macOS (window detection uses CoreGraphics — analysis works anywhere)

## Setup

### 1. Enable OBS WebSocket

Open OBS, go to **Tools > WebSocket Server Settings**, and toggle it on. Default port is `4455`. Note the password if you set one.

### 2. Get a Gemini API key

Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key.

### 3. Add to Claude Code

```bash
claude mcp add --transport stdio -s user video-feeder -- uv run /path/to/server.py
```

Then add your env vars to the MCP config in `~/.claude.json`:

```json
{
  "video-feeder": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "/path/to/server.py"],
    "env": {
      "GEMINI_API_KEY": "your-key-here"
    }
  }
}
```

### 4. Restart Claude Code

The server will appear as `video-feeder` in your MCP tools.

## App config

Create `~/.config/video-feeder/apps.json`:

```json
{
  "apps": {
    "myapp": {
      "cwd": "~/Projects/myapp",
      "command": "npm run dev",
      "window_match": "MyApp",
      "extra_logs": [".dev-logs/app.log"]
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `cwd` | Yes | Working directory for the app command |
| `command` | Yes | Shell command or argv array to launch the app |
| `window_match` | Yes | Substring to match against window owner/title (case-insensitive) |
| `cdp_url` | No | Chrome DevTools Protocol URL for console/network event capture |
| `extra_logs` | No | Additional log files to scope (absolute or relative to `cwd`) |

## Run output

Each session writes to `~/.qa-runs/video-feeder/<run_id>/`:

```
manifest.json          # session metadata, timestamps, status
recording.mp4          # the video (fragmented mp4, crash-safe)
context.md             # user-provided context
witness-report.md      # Gemini's witness report
logs/
  app.full.log         # stdout/stderr from the app process
  *.capture.log        # log lines scoped to recording interval
followups/
  followup-001.md      # ask_witness responses
  followup-002.md
```

## Usage

### Full QA session

```
you:    "record orchid, there's a flicker bug"
claude: [calls prepare_session("orchid")]
claude: [calls start_capture()]
you:    [use the app, narrate what you see]
you:    "ok stop"
claude: [calls stop_and_analyze()]
claude: "Gemini saw the toolbar flicker at 0:18, correlating with a
         React re-render in the logs. Let me look at the component..."
claude: [calls ask_witness("was the selection handle visible during the flicker?")]
claude: "Gemini confirms the handle was present but jumping between two
         positions. This matches the unstable ref in FloatingToolbar.tsx:42..."
```

### Analyze an existing file

Drop a video or screenshot into the conversation:

> "analyze this bug" + attach file path

Works without OBS.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | Yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | No | `gemini-2.5-flash` | Gemini model to use |
| `VIDEO_FEEDER_APPS_CONFIG` | No | `~/.config/video-feeder/apps.json` | App config path |
| `VIDEO_FEEDER_RUNS_DIR` | No | `~/.qa-runs/video-feeder` | Run output root |
| `VIDEO_FEEDER_MAX_CAPTURE_SECONDS` | No | `300` | Auto-stop capture limit |
| `OBS_WEBSOCKET_HOST` | No | `localhost` | OBS WebSocket host |
| `OBS_WEBSOCKET_PORT` | No | `4455` | OBS WebSocket port |
| `OBS_WEBSOCKET_PASSWORD` | No | (empty) | OBS WebSocket password |

## What OBS does automatically

Video Feeder manages OBS entirely via WebSocket — you never touch the OBS UI:

- Creates a "Video Feeder QA" profile and scene
- Attaches a ScreenCaptureKit window capture source to your app
- Scales the source to fit the canvas (handles Retina displays)
- Creates a mic input for narration audio
- Records as fragmented mp4 (crash-safe — no corrupt files on unclean stop)
- Cleans up sources after recording

## Limitations

- **macOS only** for automatic window detection and capture. You can use `analyze_bug` with existing files on any platform.
- **OBS must be installed** for recording. Analysis-only usage doesn't need it.

## License

MIT
