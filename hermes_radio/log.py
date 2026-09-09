"""Radio log at ``$HERMES_HOME/radio/radio.log``.

Format:
  16:08:18  PLAY   Mamman Sani -- Bodo  [1980s NER weird]
  16:08:22  LEVEL  ffmpeg meter started
  16:12:09  SKIP   user skip
  16:20:00  STOP   radio stopped
"""

import logging
from pathlib import Path

from . import paths

_FORMAT = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
_logger = None
_logger_path = None


def log_path() -> Path:
    return paths.radio_dir() / "radio.log"


def _get_logger() -> logging.Logger:
    """Return the radio file logger, reopening it when HERMES_HOME changed."""
    global _logger, _logger_path
    path = log_path()
    if _logger is not None and _logger_path == path:
        return _logger

    logger = logging.getLogger("hermes.radio")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_FORMAT)
    logger.addHandler(handler)

    _logger = logger
    _logger_path = path
    return logger


def play(artist: str, title: str, decade: int = 0, country: str = "", mood: str = "", source: str = "crate"):
    tag = f"[{decade}s {country} {mood}]" if decade else f"[{source}]"
    _get_logger().info("PLAY   %s -- %s  %s", artist, title, tag)


def stream(station: str, url: str = ""):
    _get_logger().info("STREAM %s  %s", station, url[:60])


def skip(reason: str = "user skip"):
    _get_logger().info("SKIP   %s", reason)


def stop(reason: str = "user stop"):
    _get_logger().info("STOP   %s", reason)


def pause(paused: bool):
    _get_logger().info("PAUSE  %s", "paused" if paused else "resumed")


def mic(text: str):
    short = text[:80] + "..." if len(text) > 80 else text
    _get_logger().info("MIC    \"%s\"", short)


def end(reason: str = "eof"):
    _get_logger().info("END    %s", reason)


def error(msg: str):
    _get_logger().error("ERROR  %s", msg)


def dig(decade: int, country: str, mood: str):
    _get_logger().info("DIG    %ds %s %s", decade, country, mood)


def meter(status: str):
    _get_logger().debug("METER  %s", status)


def info(msg: str):
    _get_logger().info("INFO   %s", msg)
