"""Hermes Radio engine: mpv, sources, crate digging, mic breaks, recording.

The daemon holds one instance and dispatches every protocol method to the same-named coroutine here.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as radio_config
from . import history, level_meter, paths
from . import log as radio_log
from .mpv_client import MpvClient
from .sources.radio_browser import RadioBrowserClient
from .sources.radio_garden import RadioGardenClient
from .sources.radiooooo import RadioooooClient
from .sources.somafm import get_channel, get_channels, get_featured
from .sources.stations import load_stations

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def _station_name_from_url(url: str) -> str:
    """Extract a readable station name from a stream URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Radio Garden long URLs carry a name slug after /listen/; short ones only an ID.
    if "radio.garden" in hostname:
        parts = [p for p in parsed.path.split("/") if p and p != "channel.mp3"]
        for i, p in enumerate(parts):
            if p == "listen" and i + 1 < len(parts):
                slug = parts[i + 1]
                if len(slug) > 10 or "-" in slug:
                    return slug.replace("-", " ").title()
        return "Radio Garden"

    if "somafm.com" in hostname:
        path = parsed.path.strip("/").split("-")[0]
        return f"SomaFM {path}" if path else "SomaFM"

    path = parsed.path.strip("/").split("/")[-1]
    if path and path not in ("stream", "channel.mp3", ""):
        return path.replace("-", " ").replace("_", " ").title()

    return hostname or "Radio"


class SourceMode(str, Enum):
    CRATE = "crate"
    STREAM = "stream"
    LOCAL = "local"


@dataclass
class NowPlaying:
    """Current playback state."""
    active: bool = False
    source_mode: str = ""
    title: str = ""
    artist: str = ""
    decade: int = 0
    country: str = ""
    mood: str = ""
    position: Optional[float] = None
    duration: Optional[float] = None
    volume: float = 80.0
    paused: bool = False
    station_name: str = ""
    recent_tracks: List[Dict[str, str]] = field(default_factory=list)


class HermesRadio:
    """The radio engine. The daemon owns exactly one."""

    def __init__(self):
        # Per-home socket paths so two Hermes homes on one machine never share an mpv.
        radio_dir = paths.radio_dir()
        self._primary = MpvClient(socket_path=str(radio_dir / "mpv-main.sock"), label="main")
        self._voice = MpvClient(socket_path=str(radio_dir / "mpv-voice.sock"), label="voice")
        self._source_mode: SourceMode = SourceMode.CRATE
        self._now = NowPlaying()
        try:
            self._now.volume = float(radio_config.get_volume())
        except Exception:
            pass
        self._crate_task: Optional[asyncio.Task] = None
        self._skip_event: Optional[asyncio.Event] = None
        self._mic_break_active = False
        self._auto_mic_breaks = True
        self._mic_break_persona = "encyclopedic"
        self._duck_volume = 15
        self._duck_ramp_ms = 500
        self._tracks_since_break = 0
        self._break_every_n = 3
        self._running = False
        self._on_state_change: Optional[Callable] = None
        self._radiooooo: Optional[RadioooooClient] = None
        self._crate_decades: Optional[List[int]] = None
        self._crate_moods: Optional[List[str]] = None
        self._crate_country: Optional[str] = None
        self._crate_weighted = True
        self._mood_weights: Optional[Dict[str, float]] = None
        self._country_weights: Optional[Dict[str, float]] = None
        self._decade_weights: Optional[Dict[int, float]] = None
        self._state_poll_task: Optional[asyncio.Task] = None
        self._muted = False
        self._pre_mute_volume = self._now.volume
        self._recording_path = ""
        self._last_icy_raw = ""
        self._unavailable_logged: set = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Spawn the primary mpv and begin polling it."""
        if self._running:
            return
        await self._primary.start()

        self._primary.on("end-file", self._on_track_end)
        self._primary.on("metadata-update", self._on_metadata_update)
        await self._primary.observe_property(1, "media-title")
        await self._primary.observe_property(2, "metadata")

        self._running = True
        await self._primary.set_volume(self._now.volume)

        self._state_poll_task = asyncio.create_task(self._poll_state())
        self._notify_state_change()
        radio_log.info("radio started")
        logger.info("Hermes Radio started")

    async def stop(self) -> str:
        """Stop playback, kill both mpv processes and the meter."""
        was_running = self._running
        self._running = False

        for task in (self._crate_task, self._state_poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._crate_task = None
        self._state_poll_task = None

        await self._primary.stop()
        if self._voice.running:
            await self._voice.stop()

        try:
            level_meter.stop()
        except Exception:
            pass

        if self._radiooooo:
            await self._radiooooo.close()
            self._radiooooo = None

        volume = self._now.volume
        self._now = NowPlaying(volume=volume)
        self._recording_path = ""
        self._muted = False
        self._notify_state_change()
        if was_running:
            radio_log.stop("radio stopped")
            logger.info("Hermes Radio stopped")
        return "Radio stopped"

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play_stream(self, url: str, station_name: str = "") -> str:
        """Play a live radio stream URL."""
        if not url:
            raise ValueError("Provide a stream URL")
        if not self._running:
            await self.start()
        self._cancel_crate()
        self._source_mode = SourceMode.STREAM
        self._now.source_mode = "stream"

        if not station_name:
            station_name = _station_name_from_url(url)

        # Switching stations closes the current recording so the file is saved.
        if self.is_recording:
            radio_log.info(await self.stop_recording())

        self._now.station_name = station_name
        self._now.title = ""
        self._now.artist = ""
        self._now.decade = 0
        self._now.country = ""
        self._now.mood = ""
        self._now.active = True
        await self._primary.loadfile(url)
        radio_log.stream(station_name, url)

        try:
            level_meter.start(url)
        except Exception:
            logger.debug("level meter failed to start", exc_info=True)

        async def _update_name():
            await asyncio.sleep(2)
            try:
                title = await self._primary.get_media_title()
                if title and title not in ("channel.mp3", url.split("/")[-1]):
                    if not self._now.station_name or self._now.station_name == _station_name_from_url(url):
                        self._now.station_name = title
                        self._notify_state_change()
                        radio_config.add_recent_station(title, url, source="stream")
            except Exception:
                pass
        asyncio.create_task(_update_name())

        try:
            history.log_station(station_name=station_name, url=url, source="stream")
            radio_config.add_recent_station(station_name, url, source="stream")
        except Exception:
            logger.debug("history write failed", exc_info=True)
        self._notify_state_change()
        return f"Tuned to {station_name}"

    async def play_station(self, query: str) -> str:
        """Play by name: curated list substring, then Radio Browser by name, then by tag."""
        q = (query or "").strip()
        if not q:
            raise ValueError("Provide a station name or URL")
        if q.startswith(("http://", "https://")):
            return await self.play_stream(q)

        ql = q.lower()
        for station in load_stations():
            name = str(station.get("name", ""))
            url = str(station.get("url", ""))
            if url and ql in name.lower():
                return await self.play_stream(url, station_name=name)

        client = RadioBrowserClient()
        try:
            stations = await client.search(name=q, limit=1)
            if not stations:
                stations = await client.search(tag=q, limit=1)
        finally:
            await client.close()
        if not stations:
            raise LookupError(f"No stations found for: {query}")
        station = stations[0]
        return await self.play_stream(station.stream_url, station_name=station.name)

    async def play_somafm(self, channel_id: str = "") -> Any:
        """Play a SomaFM channel, or list the featured channels when ``channel_id`` is empty."""
        if not channel_id:
            channels = await get_featured()
            return {"channels": [{"id": ch.id, "title": ch.title, "genre": ch.genre} for ch in channels]}
        ch = await get_channel(channel_id)
        if not ch:
            raise LookupError(f"SomaFM channel not found: {channel_id}")
        if not ch.stream_url:
            raise LookupError(f"No stream URL for channel: {channel_id}")
        return await self.play_stream(ch.stream_url, station_name=f"SomaFM {ch.title}")

    async def play_crate(
        self,
        decades: Optional[List[int]] = None,
        moods: Optional[List[str]] = None,
        country: Optional[str] = None,
        weighted: bool = True,
    ) -> str:
        """Start crate-digging from Radiooooo."""
        if not self._running:
            await self.start()
        self._cancel_crate()
        self._source_mode = SourceMode.CRATE
        self._crate_decades = decades
        self._crate_moods = moods
        self._crate_country = country
        self._crate_weighted = weighted
        self._now.source_mode = "crate"
        self._now.station_name = "Crate Digger"
        self._now.active = True

        track = await self._dig_track()
        if not track:
            return "No tracks found for those criteria"

        await self._play_track(track)
        self._crate_task = asyncio.create_task(self._crate_loop())
        self._notify_state_change()
        return f"Digging: {track.display}"

    async def play_local(self, path: str) -> str:
        """Play a local file or directory."""
        if not path:
            raise ValueError("Provide a file or directory path")
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise LookupError(f"Not found: {path}")
        if not self._running:
            await self.start()
        self._cancel_crate()
        self._source_mode = SourceMode.LOCAL
        self._now.source_mode = "local"
        self._now.station_name = ""
        self._now.active = True

        if p.is_dir():
            exts = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma", ".webm"}
            files = sorted(f for f in p.rglob("*") if f.suffix.lower() in exts)
            if not files:
                return f"No audio files found in {p}"
            await self._primary.loadfile(str(files[0]))
            for f in files[1:]:
                await self._primary.loadfile(str(f), mode="append")
            self._notify_state_change()
            return f"Playing {len(files)} tracks from {p.name}"
        await self._primary.loadfile(str(p))
        self._notify_state_change()
        return f"Playing {p.name}"

    async def skip(self) -> str:
        """Skip to the next track."""
        if not self._running:
            return "Radio is not playing"
        if self._mic_break_active:
            await self._abort_mic_break()
        if self._source_mode == SourceMode.CRATE:
            if self._skip_event:
                self._skip_event.set()
            radio_log.skip()
            return "Skipping..."
        try:
            await self._primary.playlist_next()
            radio_log.skip()
            return "Skipped"
        except Exception:
            return "Nothing to skip to"

    async def toggle_pause(self) -> str:
        if not self._running:
            return "Radio is not playing"
        await self._primary.toggle_pause()
        paused = await self._primary.is_paused()
        self._now.paused = paused
        radio_log.pause(paused)
        self._notify_state_change()
        return "Paused" if paused else "Playing"

    async def set_volume(self, level: float) -> str:
        try:
            level = float(level)
        except (TypeError, ValueError):
            raise ValueError(f"Volume must be a number, got {level!r}")
        level = max(0.0, min(100.0, level))
        if self._running:
            await self._primary.set_volume(level)
        self._now.volume = level
        self._muted = level <= 0
        if level > 0:
            self._pre_mute_volume = level
        self._notify_state_change()
        return f"Volume: {int(level)}%"

    async def adjust_volume(self, delta: float) -> str:
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            raise ValueError(f"Volume delta must be a number, got {delta!r}")
        current = await self._primary.get_volume() if self._running else self._now.volume
        return await self.set_volume(max(0.0, min(100.0, current + delta)))

    async def toggle_mute(self) -> str:
        if not self._running:
            return "Radio is not playing"
        current = await self._primary.get_volume()
        if self._muted or current <= 0:
            restore = self._pre_mute_volume if self._pre_mute_volume > 0 else 50.0
            await self._primary.set_volume(restore)
            self._now.volume = restore
            self._muted = False
            self._notify_state_change()
            return "Unmuted"

        self._pre_mute_volume = current if current > 0 else (self._pre_mute_volume or 50.0)
        await self._primary.set_volume(0)
        self._now.volume = 0
        self._muted = True
        self._notify_state_change()
        return "Muted"

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _default_recording_path(self) -> str:
        rec_dir = paths.radio_dir() / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        name_parts = []
        station = self._now.station_name or ""
        title = self._now.title or ""
        # Short alphanumeric station names are usually IDs, not names.
        if station and (len(station) > 10 or " " in station):
            name_parts.append(station)
        if title and title != station:
            name_parts.append(title)
        if not name_parts:
            name_parts.append(self._source_mode.value)

        raw_name = " - ".join(name_parts)
        safe = "".join(c for c in raw_name if c.isascii() and (c.isalnum() or c in " -_"))
        safe = safe.strip().replace(" ", "_")[:60] or "recording"
        return str(rec_dir / f"{ts}_{safe}.mp3")

    async def start_recording(self, path: str = "") -> str:
        """Record the current stream to disk via mpv's stream-record."""
        if not self._running:
            return "Radio is not playing"
        if not path:
            path = self._default_recording_path()
        try:
            await self._primary.set_property("stream-record", path)
        except Exception as e:
            return f"Recording failed: {e}"
        self._recording_path = path
        radio_log.info(f"recording started: {path}")
        self._notify_state_change()
        return f"Recording to {path}"

    async def stop_recording(self) -> str:
        if not self._recording_path:
            return "Not recording"
        path = self._recording_path
        try:
            if self._primary.running:
                await self._primary.set_property("stream-record", "")
        except Exception as e:
            return f"Stop recording failed: {e}"
        self._recording_path = ""
        radio_log.info(f"recording stopped: {path}")
        self._notify_state_change()
        return f"Recording saved: {path}"

    @property
    def is_recording(self) -> bool:
        return bool(self._recording_path)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def search(self, query: str, source: str = "radio_browser") -> Dict[str, Any]:
        """Search a source. Sources: radio_browser, somafm, radio_garden."""
        query = (query or "").strip()
        if source in ("radio_browser", "rb"):
            client = RadioBrowserClient()
            try:
                stations = await client.search(name=query, limit=10)
                if not stations:
                    stations = await client.search(tag=query, limit=10)
            finally:
                await client.close()
            return {
                "results": [
                    {"name": s.name, "country": s.country, "tags": s.tags,
                     "bitrate": s.bitrate, "url": s.stream_url}
                    for s in stations
                ],
            }

        if source == "somafm":
            channels = await get_channels()
            q = query.lower()
            matches = [
                ch for ch in channels
                if q in ch.title.lower() or q in ch.genre.lower() or q in ch.description.lower()
            ]
            return {
                "results": [
                    {"id": ch.id, "title": ch.title, "genre": ch.genre, "description": ch.description}
                    for ch in matches[:10]
                ],
            }

        if source in ("radio_garden", "rg"):
            client = RadioGardenClient()
            try:
                stations = await client.explore(query, limit=10)
            finally:
                await client.close()
            return {
                "results": [
                    {"name": s.title, "place": s.place, "country": s.country, "url": s.stream_url}
                    for s in stations
                ],
            }

        raise ValueError(f"Unknown search source: {source}")

    async def stations(self) -> Dict[str, Any]:
        """The curated station list."""
        return {"stations": load_stations()}

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """The state.json dict described in docs/protocol.md."""
        now = self._now
        active = bool(self._running)
        is_stream = active and now.source_mode == "stream"
        levels = level_meter.get_levels(64) if active else []
        return {
            "version": STATE_VERSION,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "active": active,
            "paused": bool(now.paused) if active else False,
            "muted": bool(self._muted) if active else False,
            "source_mode": now.source_mode if active else "",
            "station_name": now.station_name if active else "",
            "title": now.title if active else "",
            "artist": now.artist if active else "",
            "decade": int(now.decade or 0) if active else 0,
            "country": now.country if active else "",
            "mood": now.mood if active else "",
            "position": None if (not active or is_stream) else now.position,
            "duration": None if (not active or is_stream) else now.duration,
            "volume": int(round(now.volume)),
            "recording": self.is_recording,
            "recording_path": self._recording_path or None,
            "levels": levels,
            "meter_active": bool(active and level_meter.is_active()),
        }

    async def status(self) -> Dict[str, Any]:
        return self.snapshot()

    def now_playing(self) -> NowPlaying:
        return self._now

    def set_state_callback(self, cb: Optional[Callable]) -> None:
        """Register a callback fired on every state change."""
        self._on_state_change = cb

    def _notify_state_change(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception:
                logger.debug("state callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # Crate-dig internals
    # ------------------------------------------------------------------

    async def _get_radiooooo(self) -> RadioooooClient:
        if self._radiooooo is None:
            self._radiooooo = RadioooooClient()
        return self._radiooooo

    async def _dig_track(self):
        client = await self._get_radiooooo()
        return await client.dig(
            decades=self._crate_decades,
            moods=self._crate_moods,
            country=self._crate_country,
            weighted=self._crate_weighted,
            mood_weights=self._mood_weights,
            country_weights=self._country_weights,
            decade_weights=self._decade_weights,
        )

    async def _play_track(self, track) -> None:
        """Play a Radiooooo track and update now-playing state."""
        url = track.audio_url or track.audio_url_ogg
        if not url:
            logger.warning("Track has no audio URL: %s", track.id)
            return
        await self._primary.loadfile(url)
        try:
            level_meter.start(url)
        except Exception:
            logger.debug("level meter failed to start", exc_info=True)
        radio_log.play(track.artist, track.title, track.decade, track.country, track.mood)
        self._now.title = track.title
        self._now.artist = track.artist
        self._now.decade = track.decade
        self._now.country = track.country
        self._now.mood = track.mood
        self._now.paused = False

        self._now.recent_tracks.append({
            "title": track.title,
            "artist": track.artist,
            "decade": str(track.decade),
            "country": track.country,
            "mood": track.mood,
        })
        if len(self._now.recent_tracks) > 10:
            self._now.recent_tracks = self._now.recent_tracks[-10:]

        try:
            history.log_track(
                artist=track.artist, title=track.title, source="crate",
                decade=track.decade, country=track.country, mood=track.mood,
                duration=track.length, url=url,
            )
            history.save_track(url, track.artist, track.title, track.decade, track.country, track.mood)
            history.sync_to_honcho({
                "artist": track.artist, "title": track.title,
                "decade": track.decade, "country": track.country, "mood": track.mood,
            })
        except Exception:
            logger.debug("history write failed", exc_info=True)

        self._notify_state_change()

    async def _crate_loop(self) -> None:
        """Keep digging tracks until stopped or the source changes."""
        try:
            while self._running and self._source_mode == SourceMode.CRATE:
                prefetch_task = asyncio.create_task(self._dig_track())

                end_event = asyncio.Event()
                self._skip_event = asyncio.Event()
                load_time = time.monotonic()

                def on_end(data):
                    reason = data.get("reason", "")
                    if reason not in ("eof", "error"):
                        return
                    # loadfile fires a spurious end-file within ~2s of loading.
                    if time.monotonic() - load_time < 2.0:
                        return
                    end_event.set()

                self._primary.on("end-file", on_end)

                wait_end = asyncio.create_task(end_event.wait())
                wait_skip = asyncio.create_task(self._skip_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        [wait_end, wait_skip],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                finally:
                    if on_end in self._primary._event_callbacks.get("end-file", []):
                        self._primary._event_callbacks["end-file"].remove(on_end)
                    self._skip_event = None

                if not self._running or self._source_mode != SourceMode.CRATE:
                    prefetch_task.cancel()
                    break

                try:
                    next_track = await prefetch_task
                except asyncio.CancelledError:
                    break

                if not next_track:
                    next_track = await self._dig_track()
                    if not next_track:
                        logger.warning("Crate dig exhausted, stopping")
                        break

                await self._play_track(next_track)

                self._tracks_since_break += 1
                if self._auto_mic_breaks and self._tracks_since_break >= self._break_every_n:
                    await asyncio.sleep(2.0)
                    await self._do_mic_break(next_track)
                    self._tracks_since_break = 0

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Crate loop error")

    def _cancel_crate(self) -> None:
        if self._crate_task and not self._crate_task.done():
            self._crate_task.cancel()
        self._crate_task = None

    # ------------------------------------------------------------------
    # Mic breaks
    # ------------------------------------------------------------------

    async def mic_break(self, text: Optional[str] = None) -> str:
        """Speak ``text``, or generate commentary when omitted."""
        if not self._running:
            return "Radio is not playing"
        if self._mic_break_active:
            return "Mic break already in progress"

        if text:
            spoken = await self._speak(text)
        else:
            spoken = await self._do_mic_break()
        return "Mic break done" if spoken else "Mic breaks unavailable: Hermes TTS and LLM client not importable"

    async def _do_mic_break(self, upcoming_track=None) -> bool:
        """Generate and play a mic break with volume ducking. Returns True when audio played."""
        if self._mic_break_active:
            return False

        self._mic_break_active = True
        try:
            commentary = await self._generate_commentary(upcoming_track)
            if not commentary:
                return False

            audio_path = await self._render_tts(commentary)
            if not audio_path:
                return False
            radio_log.mic(commentary)
            await self._speak_audio(audio_path)

            try:
                history.log_mic_break(
                    commentary=commentary,
                    audio_path=audio_path,
                    track_artist=self._now.artist,
                    track_title=self._now.title,
                )
                history.save_mic_break(commentary, audio_path)
            except Exception:
                logger.debug("mic break archive failed", exc_info=True)
            return True
        except Exception:
            logger.exception("Mic break error")
            return False
        finally:
            self._mic_break_active = False

    async def _speak(self, text: str) -> bool:
        self._mic_break_active = True
        try:
            audio_path = await self._render_tts(text)
            if not audio_path:
                return False
            radio_log.mic(text)
            await self._speak_audio(audio_path)
            return True
        finally:
            self._mic_break_active = False

    async def _speak_audio(self, audio_path: str) -> None:
        """Play a rendered clip on the voice mpv while ducking the music."""
        if not self._voice.running:
            await self._voice.start()

        await self._primary.ramp_volume(self._duck_volume, duration_ms=self._duck_ramp_ms)

        await self._voice.set_volume(65)
        await self._voice.loadfile(audio_path)

        voice_end = asyncio.Event()

        def on_voice_end(data):
            voice_end.set()

        self._voice.on("end-file", on_voice_end)
        try:
            await asyncio.wait_for(voice_end.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if on_voice_end in self._voice._event_callbacks.get("end-file", []):
                self._voice._event_callbacks["end-file"].remove(on_voice_end)

        await self._primary.ramp_volume(self._now.volume, duration_ms=self._duck_ramp_ms)

    async def _abort_mic_break(self) -> None:
        if self._voice.running:
            await self._voice.stop()
        await self._primary.set_volume(self._now.volume)
        self._mic_break_active = False

    def _log_unavailable(self, key: str, message: str) -> None:
        if key in self._unavailable_logged:
            return
        self._unavailable_logged.add(key)
        radio_log.info(message)
        logger.warning(message)

    async def _render_tts(self, text: str) -> Optional[str]:
        """Render text to audio with Hermes TTS. None when the tool is not importable."""
        try:
            from tools.tts_tool import text_to_speech_tool
        except Exception:
            self._log_unavailable("tts", "mic breaks unavailable: tools.tts_tool not importable (set hermes_root)")
            return None
        try:
            result_json = await asyncio.get_running_loop().run_in_executor(
                None, lambda: text_to_speech_tool(text=text)
            )
            result = json.loads(result_json)
            if result.get("success"):
                return result["file_path"]
            logger.warning("TTS failed: %s", result.get("error"))
            return None
        except Exception:
            logger.exception("TTS render error")
            return None

    async def _generate_commentary(self, upcoming_track=None) -> Optional[str]:
        """Generate DJ commentary with the Hermes auxiliary LLM. None when it is not importable."""
        try:
            from agent.auxiliary_client import call_llm
        except Exception:
            self._log_unavailable("llm", "mic breaks unavailable: agent.auxiliary_client not importable (set hermes_root)")
            return None

        now = self._now
        hour = time.localtime().tm_hour
        if hour < 6:
            time_vibe = "late night"
        elif hour < 12:
            time_vibe = "morning"
        elif hour < 18:
            time_vibe = "afternoon"
        else:
            time_vibe = "evening"

        current = f"{now.artist} - {now.title}" if now.title else "unknown"
        recent = "; ".join(
            f"{t['artist']} - {t['title']} ({t['decade']}s, {t['country']})"
            for t in now.recent_tracks[-5:]
        )

        upcoming_info = ""
        if upcoming_track:
            upcoming_info = f"\nComing up next: {upcoming_track.artist} - {upcoming_track.title} ({upcoming_track.decade}s, {upcoming_track.country}, {upcoming_track.mood})"

        persona_prompts = {
            "encyclopedic": "You're a deeply knowledgeable music historian DJ. Share fascinating context about the music -- provenance, cultural significance, recording history, the label, the scene.",
            "deadpan": "You're a dry, sardonic late-night DJ. Minimal words, maximum effect. Understated observations.",
            "enthusiastic": "You're an infectiously excited college radio DJ discovering music for the first time. Genuinely thrilled.",
            "conspiratorial": "You're a paranoid late-night DJ who sees hidden connections between every track. Everything is linked.",
        }
        persona = persona_prompts.get(self._mic_break_persona, persona_prompts["encyclopedic"])

        prompt = f"""{persona}

You're doing a mic break on Hermes Radio. It's {time_vibe}. Keep it to 1-3 sentences. Conversational, not scripted. Never mention being an AI. No hashtags.

Just played: {current}
Recent history: {recent}{upcoming_info}
Source: {now.source_mode}"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: call_llm(
                    task="radio",
                    messages=messages,
                    max_tokens=150,
                    temperature=0.9,
                    timeout=10.0,
                ),
            )
            text = response.choices[0].message.content
            if text and text.strip():
                return text.strip()
        except Exception:
            logger.debug("LLM commentary generation failed, using template", exc_info=True)

        if upcoming_track:
            return f"That was {now.artist}. Coming up, {upcoming_track.artist} from the {upcoming_track.decade}s."
        return f"You're listening to Hermes Radio. That was {now.artist} -- {now.title}."

    # ------------------------------------------------------------------
    # mpv event handlers
    # ------------------------------------------------------------------

    async def _poll_state(self) -> None:
        """Refresh NowPlaying from mpv every ~300ms."""
        try:
            while self._running:
                try:
                    if self._primary.running and not await self._primary.is_idle():
                        self._now.position = await self._primary.get_position()
                        self._now.duration = await self._primary.get_duration()
                        self._now.volume = await self._primary.get_volume()
                        self._muted = self._now.volume <= 0

                        if self._source_mode == SourceMode.STREAM:
                            try:
                                title = await self._primary.get_media_title()
                                if title and title != self._last_icy_raw and title not in ("channel.mp3",):
                                    self._last_icy_raw = title
                                    self._now.title = title
                                    self._now.artist = ""
                                    self._notify_state_change()
                            except Exception:
                                pass
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass

    def _on_track_end(self, data: dict) -> None:
        # The crate loop registers its own end-file listener.
        pass

    def _on_metadata_update(self, data: dict) -> None:
        if self._source_mode == SourceMode.STREAM:
            asyncio.create_task(self._refresh_stream_metadata())

    async def _refresh_stream_metadata(self) -> None:
        try:
            title = await self._primary.get_media_title()
            if title and title != self._now.title:
                self._now.title = title
                if " - " in title:
                    parts = title.split(" - ", 1)
                    self._now.artist = parts[0].strip()
                    self._now.title = parts[1].strip()
                else:
                    self._now.artist = ""
                self._notify_state_change()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def configure(self, config: dict) -> None:
        """Apply the ``radio:`` section of the main Hermes config.yaml."""
        radio_cfg = config.get("radio", {})
        mic_cfg = radio_cfg.get("mic_breaks", {})
        crate_cfg = radio_cfg.get("crate", {})

        self._now.volume = radio_cfg.get("default_volume", self._now.volume)
        self._auto_mic_breaks = mic_cfg.get("enabled", True)
        self._mic_break_persona = mic_cfg.get("persona", "encyclopedic")
        self._duck_volume = mic_cfg.get("duck_volume", 20)
        self._duck_ramp_ms = mic_cfg.get("duck_ramp_ms", 800)

        freq = mic_cfg.get("frequency", "every_track")
        if freq == "every_track":
            self._break_every_n = 1
        elif freq == "manual":
            self._auto_mic_breaks = False
        else:
            self._break_every_n = mic_cfg.get("interval", 3)

        self._crate_decades = crate_cfg.get("decades")
        self._crate_moods = crate_cfg.get("moods")
        self._crate_country = None
        self._crate_weighted = crate_cfg.get("weighted", True)

        mood_w = crate_cfg.get("mood_weights")
        if mood_w and isinstance(mood_w, dict):
            self._mood_weights = {str(k): float(v) for k, v in mood_w.items()}

        country_w = crate_cfg.get("country_weights")
        if country_w and isinstance(country_w, dict):
            self._country_weights = {str(k).upper(): float(v) for k, v in country_w.items()}

        decade_w = crate_cfg.get("decade_weights")
        if decade_w and isinstance(decade_w, dict):
            self._decade_weights = {int(k): float(v) for k, v in decade_w.items()}
