# Hermes Radio: process model and protocol

Hermes Radio runs as one detached daemon per Hermes home. The daemon owns mpv,
the ffmpeg level meter, the crate-dig loop, and recording. Every UI is a thin
client: the agent tools, the `/radio` slash command, the `hermes radio` CLI
subcommand, the classic-CLI wrapper, and the Ink TUI dock widget.

Why a daemon: in `hermes --tui` the agent tools run in the Python gateway
process and slash commands run in a separate slash-worker process that
restarts on model switch. An in-process player would spawn two mpvs and die
on restart. One daemon, reached over a socket, gives every surface the same
player. It also means the music keeps playing when you restart Hermes.

## Files

All under `RADIO_DIR = $HERMES_HOME/radio` (default `~/.hermes/radio`).
`hermes_radio.paths.radio_dir()` is the only place that computes it.

| Path | Writer | Purpose |
|---|---|---|
| `control.sock` | daemon | Unix socket, newline-delimited JSON requests |
| `daemon.pid` | daemon | PID of the running daemon |
| `state.json` | daemon | Now-playing snapshot, rewritten atomically at ~6 Hz while active, and once on every state change |
| `daemon.json` | plugin `register()` | Launcher spec so non-Python clients (the Ink widget) can start the daemon |
| `config.yaml` | clients | User settings: volume, visualizer, decades, moods, presets, recent stations |
| `stations.yaml` | user | Overrides the bundled curated station list |
| `visualizers/*.yaml` | user | Extra visualizer presets |
| `history.jsonl`, `tracks/`, `recordings/`, `radio.log` | daemon | Optional archives and the radio log |

## `state.json`

```json
{
  "version": 1,
  "pid": 48213,
  "updated_at": 1757433600.123,
  "active": true,
  "paused": false,
  "muted": false,
  "source_mode": "stream",
  "station_name": "NTS Radio 1",
  "title": "Live from London",
  "artist": "",
  "decade": 0,
  "country": "",
  "mood": "",
  "position": null,
  "duration": null,
  "volume": 80,
  "recording": false,
  "recording_path": null,
  "levels": [0.12, 0.31, 0.44],
  "meter_active": true
}
```

- `source_mode` is `stream`, `crate`, or `local`.
- `position` and `duration` are seconds for finite tracks and `null` for streams.
- `levels` holds up to 64 normalized RMS samples in `[0, 1]`, oldest first.
  Clients render the visualizer from this list. Empty when the meter is off.
- A client treats the radio as inactive when the file is missing, when
  `active` is false, or when `updated_at` is older than 3 seconds and the PID
  in the file is dead.

## `daemon.json`

```json
{"python": "/path/to/venv/bin/python", "script": "/path/to/plugin/hermes_radio/daemon.py", "hermes_root": "/path/to/hermes-agent"}
```

The plugin writes this on `register()`. A client that finds no live socket
spawns `python script` detached, with `hermes_root` prepended to `PYTHONPATH`
so the daemon can import the Hermes Honcho tools for the optional
listening-history sync. That import is optional. Without it the sync logs
once and everything else works.

## `control.sock`

One JSON object per line in each direction.

Request: `{"id": 1, "method": "play_stream", "params": {"url": "https://..."}}`

Reply: `{"id": 1, "ok": true, "result": "Playing NTS Radio 1"}`
or `{"id": 1, "ok": false, "error": "mpv not found in PATH"}`

The daemon handles requests concurrently. A client may keep the connection
open and pipeline requests, or open one connection per call.

### Methods

| Method | Params | Result |
|---|---|---|
| `ping` | | `{"pid": int, "active": bool}` |
| `status` | | the `state.json` dict |
| `play_stream` | `url`, `station_name=""` | message string |
| `play_station` | `query` | message string. Matches the curated list by name substring first, then Radio Browser by name, then by tag. |
| `play_somafm` | `channel_id=""` | message string, or `{"channels": [...]}` when `channel_id` is empty |
| `play_crate` | `decades=None`, `moods=None`, `country=None` | message string |
| `play_local` | `path` | message string |
| `skip` | | message string |
| `toggle_pause` | | message string |
| `toggle_mute` | | message string |
| `set_volume` | `level` (0-100) | message string |
| `adjust_volume` | `delta` | message string |
| `start_recording` | `path=""` | message string |
| `stop_recording` | | message string |
| `search` | `query`, `source="radio_browser"` | `{"results": [...]}`; sources: `radio_browser`, `somafm`, `radio_garden` |
| `stations` | | `{"stations": [...]}` the curated list |
| `stop` | | message string. Stops playback, kills mpv, replies, then the daemon exits. |

Unknown method: `{"ok": false, "error": "unknown method: x"}`.

## Client API (`hermes_radio.client`)

Synchronous. Safe to call from any thread and from inside a running asyncio
loop because it uses blocking sockets, never the caller's loop.

```python
radio_dir() -> Path
daemon_running() -> bool                 # socket connects and ping succeeds
ensure_daemon(timeout: float = 8.0)      # spawns from daemon.json when needed; raises RadioUnavailable
call(method: str, *, timeout: float = 30.0, start: bool = True, **params)
                                         # raises RadioError(message) on ok=false, RadioUnavailable when no daemon and start=False
read_state() -> dict                     # inactive-shaped dict when missing or stale
write_launcher(python: str, script: str, hermes_root: str | None)
```

`RadioUnavailable` and `RadioError` both subclass `Exception`.

## Slash command grammar (`hermes_radio.commands.run(raw_args) -> str`)

```
/radio                       status line, or "Radio is off. Try /radio play nts" when inactive
/radio play <name|url>       play_station or play_stream
/radio soma [channel]        play_somafm
/radio crate [1970 JPN slow] play_crate; tokens: 4-digit decade, 3-letter country, slow|fast|weird
/radio local <path>          play_local
/radio pause                 toggle_pause
/radio skip | next           skip
/radio mute                  toggle_mute
/radio vol <0-100> | +N | -N set_volume / adjust_volume
/radio rec [start|stop]      start_recording / stop_recording (toggle when omitted)
/radio search <query>        search, radio_browser
/radio stations              curated list
/radio viz [name|next|prev]  visualizer preset (client-side config, no daemon call)
/radio stop                  stop
/radio help                  this grammar
```

Every branch returns plain text with no ANSI, because the TUI renders the
result as one system line.

## Keybinding

`Ctrl+R` in the classic CLI wrapper (`hermes-radio`). When the radio is off it
opens the station menu. When the radio is on it toggles control mode:

```
Space pause   n skip   m mute   - / + volume   r record
< / > visualizer   Tab expand   Enter menu   q / Esc / Ctrl+R exit
```

The Ink TUI has no keybinding registry for widgets, so there `/radio` is the
entry point and the dock widget is display only until upstream adds one.
