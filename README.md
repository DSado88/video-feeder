# Video Feeder

An MCP server that records QA sessions via OBS, scopes app logs to the capture window, and analyzes the evidence with Gemini. Built for Claude Code-style local QA loops.

You know that bug you can't describe? Record it, narrate it, and give the coding agent video, audio, logs, and timeline context.

## How it works

1. Configure the app once in `~/.config/video-feeder/apps.json`
2. Ask Claude to prepare a QA session for that app
3. Video Feeder launches the app with tee logging, opens OBS, and attaches a window capture
4. You start capture, reproduce the bug, narrate what you see, and stop capture
5. Video Feeder slices logs to the capture interval, includes optional CDP events, uploads the video plus evidence packet to Gemini, and returns a structured analysis

No context switching. No "let me try to explain what happens when I..."

## Tools

| Tool | What it does |
|------|-------------|
| `list_apps` | Shows configured apps from `~/.config/video-feeder/apps.json` |
| `prepare_session` / `qa_prepare` | Launches the app, creates a run folder, prepares OBS capture |
| `start_capture` / `qa_record` | Starts OBS recording and marks the log/CDP evidence boundary |
| `stop_and_analyze` / `qa_stop` | Stops capture, slices evidence, sends video + packet to Gemini |
| `list_windows` | Shows all capturable windows with IDs (macOS) |
| `start_recording` | Legacy/simple mode: creates an OBS window capture and starts recording |
| `analyze_bug` / `qa_analyze` | Analyze an existing video/screenshot (no OBS needed) |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package runner)
- [OBS Studio](https://obsproject.com/) with WebSocket server enabled
- A [Gemini API key](https://aistudio.google.com/apikey)
- macOS (window detection uses CoreGraphics — OBS control and analysis work anywhere)

## Setup

### 1. Enable OBS WebSocket

Open OBS, go to **Tools > WebSocket Server Settings**, and toggle it on. Default port is `4455`. Note the password if you set one.

### 2. Get a Gemini API key

Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key. Free tier gives you ~20 requests/day.

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
    "orchid": {
      "cwd": "/Users/david/Documents/Programs/Orchid",
      "command": "cd frontend && npm run tauri dev",
      "window_match": "Orchid",
      "cdp_url": "http://localhost:9222",
      "extra_logs": [
        "frontend/.dev-logs/tauri-dev.log"
      ]
    }
  }
}
```

`command` may be a shell string or an argv array. `extra_logs` are optional and may be absolute or relative to `cwd`.

Runs are written to:

```text
~/.qa-runs/video-feeder/<run_id>/
  manifest.json
  recording.*
  context.md
  cdp-events.ndjson
  logs/
    app.full.log
    app.full.log.capture.log
  analysis.md
```

## Usage

### Full QA session

Tell Claude:

> `/qa prepare orchid`

Then:

> `/qa record`

Reproduce the bug while narrating what you see, then:

> `/qa stop`

In MCP terms, those slash commands map to `qa_prepare`, `qa_record`, and `qa_stop`.

`/qa stop` cleans up the Video Feeder run state and OBS capture source, but it does not kill the app process launched by `/qa prepare`. This is intentional so your dev server/app stays open after QA.

### Simple legacy recording

You can still say:

> "gonna record a bug"

Claude can call `list_windows`, `start_recording`, wait for you to reproduce, then call `stop_and_analyze` without a `run_id`.

### Analyze an existing file

Drop a video or screenshot into the conversation:

> "analyze this bug" + attach file

### With project context

The more context you give, the better the analysis:

> "gonna record a bug — working on a React app, seeing a rendering glitch when I scroll the sidebar"

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | Yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | No | `gemini-2.5-flash` | Gemini model to use |
| `VIDEO_FEEDER_APPS_CONFIG` | No | `~/.config/video-feeder/apps.json` | App config path |
| `VIDEO_FEEDER_RUNS_DIR` | No | `~/.qa-runs/video-feeder` | QA run output root |
| `VIDEO_FEEDER_MAX_CAPTURE_SECONDS` | No | `300` | Auto-stop capture limit |
| `VIDEO_FEEDER_GEMINI_PROCESSING_TIMEOUT_SECONDS` | No | `300` | Gemini video-processing poll timeout |
| `OBS_WEBSOCKET_HOST` | No | `localhost` | OBS WebSocket host |
| `OBS_WEBSOCKET_PORT` | No | `4455` | OBS WebSocket port |
| `OBS_WEBSOCKET_PASSWORD` | No | (empty) | OBS WebSocket password |

## How the analysis works

Video Feeder sends your recording to Gemini with a prompt grounded in the assumption that **the developer can't describe the bug** — that's why they're recording it. In full QA-session mode, Gemini also receives:

- `manifest.json` with capture start/stop timestamps
- microphone narration inside the recording, if OBS captured it
- app logs sliced by byte offset to the recording interval
- timestamp-filtered JSONL logs when available
- optional CDP console/runtime/network/navigation events
- repository and user-provided context

Gemini is told to:

1. Describe exactly what it sees, step by step with timestamps
2. Correlate visible behavior with logs/CDP events
3. Identify the bug or unexpected behavior
4. Note any error messages or UI state
5. Suggest likely root causes
6. Ask targeted follow-up questions only if needed

The response is structured to be actionable by an AI coding assistant, so Claude can immediately start working on the fix.

## Limitations

- **macOS only** for window detection (`list_windows`, `start_recording` with `window_name`). You can still use `start_recording` with a known `window_id` on other platforms, or use `analyze_bug` with existing files anywhere.
- **OBS must be running** for recording tools. The `analyze_bug` tool works without OBS.
- **Free Gemini tier** is limited to ~20 requests/day. Set `GEMINI_MODEL` to try different models.

## License

MIT
