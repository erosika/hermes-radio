# hermes-radio

Internet radio for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Curated world stations, SomaFM, Radio Garden, Radiooooo crate digging by
decade and mood, Radio Browser search, dub-to-disk recording, and a braille
visualizer, all as a Hermes plugin with no core changes.

One detached mpv daemon does the playing. Every Hermes surface talks to it:

| Surface | What you get |
|---|---|
| Agent tools | `radio_play`, `radio_pause`, `radio_stop`, `radio_skip`, `radio_status`, `radio_volume`, `radio_record`, `radio_search` in the `radio` toolset |
| `/radio ...` | Slash command in the classic CLI, the Ink TUI, and gateway chats |
| `hermes radio ...` | Same grammar from the shell |
| `hermes-radio` | Classic CLI with a mini player under the input and `Ctrl+R` transport controls |
| Ink TUI (`hermes --tui`) | A dock card between the input and the status bar with live bars and now playing |
| Telegram / Discord | `radio_play source=crate` downloads a track and sends it as media instead of playing |

## Requirements

- Hermes Agent, September 2026 or later (the plugin API with `register_command` and the CLI extension hooks).
- `mpv` and `ffmpeg` on `PATH`. On macOS: `brew install mpv ffmpeg`. ffmpeg is optional and only drives the level meter.
- Python 3.11 or later, `httpx`, `pyyaml`. Both ship with Hermes.

## Install

```sh
hermes plugins install erosika/hermes-radio
hermes plugins enable hermes-radio
```

The plugin is `kind: standalone`, so the enable step is required.

For the Ink TUI dock card, link the widget file into the widgets directory Hermes watches:

```sh
mkdir -p ~/.hermes/tui-widgets
ln -s ~/.hermes/plugins/hermes-radio/tui-widgets/radio.mjs ~/.hermes/tui-widgets/radio.mjs
```

For the classic CLI with `Ctrl+R`, install the wrapper entry point into the Hermes environment:

```sh
uv pip install --python ~/.hermes/hermes-agent/.venv/bin/python -e ~/.hermes/plugins/hermes-radio
~/.hermes/hermes-agent/.venv/bin/hermes-radio
```

Adjust the path if your Hermes checkout lives elsewhere. `hermes-radio` accepts the same flags as `hermes`.

## Use

```
/radio play nts               curated station or Radio Browser match
/radio play https://…         any stream URL
/radio soma dronezone         SomaFM channel; /radio soma lists them
/radio crate 1970 JPN slow    Radiooooo crate dig
/radio pause  skip  mute
/radio vol 60   vol +5   vol -5
/radio rec                    record the stream to ~/.hermes/radio/recordings/
/radio search lagos
/radio viz braille            visualizer preset: blocks, braille, scatter, or your own
/radio stop                   stop playback and the daemon
```

Or ask the agent: "play some 70s Japanese slow stuff", "what's playing", "turn it down".

### Ctrl+R in `hermes-radio`

Radio off: `Ctrl+R` opens the station menu. Radio on: `Ctrl+R` toggles control mode.

```
Space pause   n skip   m mute   - / + volume   r record
< / > visualizer   Tab expand   Enter menu   q / Esc / Ctrl+R exit
```

Control mode only fires when no Hermes modal prompt is open, and it releases every key the moment you leave it.

## Files

Everything mutable lives under `~/.hermes/radio` (or `$HERMES_HOME/radio`).

| Path | Purpose |
|---|---|
| `config.yaml` | volume, visualizer, crate decades and moods, recent stations, presets |
| `stations.yaml` | your curated list; overrides the bundled one |
| `visualizers/*.yaml` | your visualizer presets |
| `recordings/`, `tracks/`, `history.jsonl` | archives, each gated by a config flag |
| `state.json`, `control.sock`, `daemon.pid`, `daemon.json`, `radio.log` | daemon runtime |

## How it works

```
 hermes (classic)   hermes --tui gateway   slash worker   hermes-radio   radio.mjs
      tools              tools               /radio        Ctrl+R UI     dock card
        │                  │                   │              │             │
        └──────────────────┴─────────┬─────────┴──────────────┴─────────────┘
                                     │  control.sock  (JSON lines)
                                     │  state.json    (6 Hz snapshot)
                              ┌──────┴──────┐
                              │ radio daemon│  mpv (IPC)  ·  ffmpeg level meter
                              └─────────────┘
```

In `hermes --tui`, tools run in the Python gateway process and slash commands
run in a separate worker process that restarts on model switch. An in-process
player would give those two surfaces two different mpvs. The daemon gives
them one, and the music keeps playing when you restart Hermes.

The protocol is in [`docs/protocol.md`](docs/protocol.md).

## Development

```sh
git clone https://github.com/erosika/hermes-radio ~/.hermes/plugins/hermes-radio
cd ~/.hermes/plugins/hermes-radio
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m pytest tests -q
```

The daemon can run on its own for debugging:

```sh
python hermes_radio/daemon.py
hermes radio play nts
```

## Status

- Ink TUI: the dock card and `/radio` work today. Widgets cannot register
  keybindings in the TUI yet, so `Ctrl+R` there waits on an upstream hook.
- Desktop app: not wired. The desktop plugin SDK supports panes and keybinds,
  so a mini player there is possible as a follow-up.
- No DJ. The March branch had TTS mic breaks between crate tracks. This
  plugin plays music and nothing that talks.

## History

This started as [hermes-agent#1466](https://github.com/NousResearch/hermes-agent/pull/1466),
a native player wired into `cli.py`. It closed with the suggestion to ship it
as a standalone plugin. This is that plugin.
