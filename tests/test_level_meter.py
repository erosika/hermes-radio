import pytest

from hermes_radio import level_meter
from hermes_radio.level_meter import VisualizerFeatures, features_from_levels


def test_empty_levels_are_inactive():
    f = features_from_levels([], 8)
    assert isinstance(f, VisualizerFeatures)
    assert f.active is False
    assert f.levels == [0.0] * 8
    assert f.energy == f.peak == f.transient == f.motion == f.decay == 0.0


def test_constant_levels_have_no_motion():
    f = features_from_levels([0.5] * 10, 16)
    assert f.active is True
    assert len(f.levels) == 16
    assert all(abs(v - 0.5) < 1e-9 for v in f.levels)
    assert abs(f.energy - 0.5) < 1e-9
    assert abs(f.peak - 0.5) < 1e-9
    assert f.transient == 0.0
    assert f.motion == 0.0
    assert f.decay == 0.0


def test_rising_levels_have_transient_and_no_decay():
    f = features_from_levels([0.1, 0.1, 0.1, 0.9], 4)
    assert f.transient > 0.0
    assert f.decay == 0.0
    assert f.peak == pytest.approx(0.9)


def test_falling_levels_have_decay_and_no_transient():
    f = features_from_levels([0.9, 0.9, 0.9, 0.1], 4)
    assert f.decay > 0.0
    assert f.transient == 0.0


def test_input_is_clamped_and_resampled():
    f = features_from_levels([-1.0, 2.0], 5)
    assert len(f.levels) == 5
    assert all(0.0 <= v <= 1.0 for v in f.levels)
    assert f.levels[0] == 0.0
    assert f.levels[-1] == 1.0


def test_width_is_at_least_one():
    assert len(features_from_levels([0.3], 0).levels) == 1


def test_smoothing_keeps_length_and_range():
    raw = [0.0, 1.0] * 8
    f = features_from_levels(raw, 16, smoothing=0.5)
    assert len(f.levels) == 16
    assert all(0.0 <= v <= 1.0 for v in f.levels)
    assert max(f.levels) < 1.0


def test_snapshot_without_meter_is_inactive():
    assert level_meter.is_active() is False
    f = level_meter.get_feature_snapshot(12)
    assert f.active is False
    assert f.levels == [0.0] * 12


def test_resolve_playlist_passthrough_for_streams():
    from hermes_radio.level_meter import _resolve_playlist
    assert _resolve_playlist("https://ice2.somafm.com/dronezone-128-mp3") == "https://ice2.somafm.com/dronezone-128-mp3"


def test_resolve_playlist_reads_first_pls_entry(monkeypatch):
    from hermes_radio import level_meter
    import httpx

    class Reply:
        text = "[playlist]\nNumberOfEntries=2\nFile1=https://ice1.somafm.com/dronezone-256-mp3\nTitle1=SomaFM\nFile2=https://ice2.somafm.com/dronezone-256-mp3\n"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())
    assert level_meter._resolve_playlist("https://somafm.com/dronezone256.pls") == "https://ice1.somafm.com/dronezone-256-mp3"


def test_resolve_playlist_reads_first_m3u_entry(monkeypatch):
    from hermes_radio import level_meter
    import httpx

    class Reply:
        text = "#EXTM3U\n#EXTINF:-1,NTS\nhttp://stream-relay-geo.ntslive.net/stream\n"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())
    assert level_meter._resolve_playlist("https://example.org/nts.m3u") == "http://stream-relay-geo.ntslive.net/stream"
