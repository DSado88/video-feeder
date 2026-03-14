# Video Feeder

An MCP server that records bugs via OBS and analyzes them with Gemini. Built for Claude Code.

You know that bug you can't describe? Record it, and let Gemini explain it for you.

## How it works

1. You tell Claude you want to record a bug
2. Video Feeder detects your windows, picks the right one, and tells OBS to start recording
3. You reproduce the bug and say "done"
4. Video Feeder stops recording, uploads to Gemini with your project context, and returns a structured analysis with likely root causes and follow-up questions

No context switching. No "let me try to explain what happens when I..."

## Tools

| Tool | What it does |
|------|-------------|
| `list_windows` | Shows all capturable windows with IDs (macOS) |
| `start_recording` | Creates an OBS window capture and starts recording |
| `stop_and_analyze` | Stops recording, sends to Gemini, returns analysis |
| `analyze_bug` | Analyze an existing video/screenshot (no OBS needed) |

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

## Usage

### Record a bug

Just tell Claude:

> "gonna record a bug"

Claude will detect your active window, start OBS recording, wait for you to reproduce, then analyze.

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
| `OBS_WEBSOCKET_HOST` | No | `localhost` | OBS WebSocket host |
| `OBS_WEBSOCKET_PORT` | No | `4455` | OBS WebSocket port |
| `OBS_WEBSOCKET_PASSWORD` | No | (empty) | OBS WebSocket password |

## How the analysis works

Video Feeder sends your recording to Gemini with a prompt grounded in the assumption that **the developer can't describe the bug** — that's why they're recording it. Gemini is told to:

1. Describe exactly what it sees, step by step with timestamps
2. Identify the bug or unexpected behavior
3. Note any error messages or UI state
4. Suggest likely root causes
5. Ask 2-3 targeted follow-up questions

The response is structured to be actionable by an AI coding assistant, so Claude can immediately start working on the fix.

## Limitations

- **macOS only** for window detection (`list_windows`, `start_recording` with `window_name`). You can still use `start_recording` with a known `window_id` on other platforms, or use `analyze_bug` with existing files anywhere.
- **OBS must be running** for recording tools. The `analyze_bug` tool works without OBS.
- **Free Gemini tier** is limited to ~20 requests/day. Set `GEMINI_MODEL` to try different models.

## License

MIT
