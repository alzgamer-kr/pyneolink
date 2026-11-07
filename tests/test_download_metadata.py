from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyneolink.sd_card import SDFileCollection, SdCard


@pytest.fixture
def download_stub(monkeypatch):
    camera = SimpleNamespace(config=SimpleNamespace(channel_id=0))
    sd = SdCard(camera)
    calls = []

    def download_once(item, raw, file_id, output, **kwargs):
        calls.append(output)
        output.write_bytes(b"finalized")
        os.utime(output, ns=(1_600_000_000_000_000_000, 1_600_000_001_000_000_000))
        return output

    monkeypatch.setattr(sd, "_download_once", download_once)
    return sd, calls


@pytest.mark.parametrize(
    "started",
    [
        datetime(2026, 1, 15, 12, 34, 56),
        datetime(2026, 7, 15, 12, 34, 56),
        datetime(2026, 9, 10, 12, 34, 56, tzinfo=timezone(timedelta(hours=3))),
    ],
)
def test_download_mtime_uses_recording_time_and_preserves_atime(download_stub, tmp_path, started):
    sd, calls = download_stub
    path = sd.file({"file_name": "clip.mp4", "start_time": started.isoformat()}).download(tmp_path)
    assert calls == [path]
    assert path.name == "clip.mp4"
    assert path.stat().st_mtime == pytest.approx(started.timestamp(), rel=0, abs=0.001)
    assert path.stat().st_atime_ns == 1_600_000_000_000_000_000


def test_template_and_skip_reuse_same_path(download_stub, tmp_path):
    sd, calls = download_stub
    started = datetime(2026, 9, 10, 12, 34, 56)
    file = sd.file({"file_name": "clip.mp4", "start_time": started, "channel_id": 0})
    template = "{start_time:%Y-%m-%d_%H-%M-%S}_{channel_id}_{stem}"
    path = file.download(tmp_path / "new", filename_template=template)
    assert path == tmp_path / "new" / "2026-09-10_12-34-56_0_clip.mp4"
    os.utime(path, (1_600_000_000, 1_600_000_001))
    result = SDFileCollection(sd, [file]).download(tmp_path / "new", filename_template=template, rewrite_exists=False)
    assert result.ok
    assert result.skipped == 1
    assert calls == [path]
    assert path.stat().st_mtime == pytest.approx(started.timestamp(), rel=0, abs=0.001)


@pytest.mark.parametrize("template", ["{name}", "{stem}.mp4"])
def test_template_does_not_duplicate_extension(download_stub, tmp_path, template):
    sd, _ = download_stub
    assert sd.file({"file_name": "clip.mp4"}).download(tmp_path, filename_template=template).name == "clip.mp4"


@pytest.mark.parametrize("metadata", [{}, {"start_time": "bad"}, {"raw": {"beginTime": {}}}])
def test_missing_date_keeps_download_mtime(download_stub, tmp_path, metadata):
    sd, _ = download_stub
    path = sd.file({"file_name": "clip.mp4", **metadata}).download(tmp_path)
    assert path.stat().st_mtime_ns == 1_600_000_001_000_000_000


def test_raw_metadata_time_and_name(download_stub, tmp_path):
    sd, _ = download_stub
    path = sd.file(
        {
            "name": "raw.mp4",
            "raw": {"beginTime": {"year": 2026, "month": 9, "day": 10, "hour": 10, "minute": 20, "second": 30}},
        }
    ).download(tmp_path)
    assert path.name == "raw.mp4"
    assert path.stat().st_mtime == pytest.approx(datetime(2026, 9, 10, 10, 20, 30).timestamp(), rel=0, abs=0.001)


@pytest.mark.parametrize(
    "template",
    [
        "",
        ".",
        "..",
        " ",
        "../escape",
        "nested/file",
        "nested\\file",
        "C:escape",
        "CON",
        "LPT1.mp4",
        "{unknown}",
        "{name.__class__}",
        "{name[0]}",
        "{name!r}",
        "{name:{stem}}",
        "{start_time:%Y}",
        "{end_time:%Y}",
        "{channel_id}",
        "{",
        "bad\nname",
    ],
)
def test_invalid_template_fails_before_download(download_stub, tmp_path, template):
    sd, calls = download_stub
    with pytest.raises(ValueError):
        sd.file({"file_name": "clip.mp4"}).download(tmp_path, filename_template=template)
    assert not calls
    assert not list(tmp_path.iterdir())


def test_explicit_output_path_still_works_without_template(download_stub, tmp_path):
    sd, _ = download_stub
    path = sd.file({"file_name": "clip.mp4"}).download(tmp_path / "custom.mp4")
    assert path == tmp_path / "custom.mp4"
    with pytest.raises(ValueError, match="directory"):
        sd.file({"file_name": "clip.mp4"}).download(path, filename_template="{stem}")


def test_duplicate_template_names_rejected_before_any_download(download_stub, tmp_path):
    sd, calls = download_stub
    files = SDFileCollection(
        sd, [sd.file({"file_name": name, "start_time": "2026-09-10T12:00:00"}) for name in ("one.mp4", "two.mp4")]
    )
    with pytest.raises(ValueError, match="Duplicate"):
        files.download(tmp_path, filename_template="{start_time:%Y-%m-%d_%H-%M-%S}")
    assert not calls
    result = files.download(tmp_path, filename_template="{start_time:%Y-%m-%d_%H-%M-%S}_{stem}")
    assert result.ok
    assert result.completed == 2


def test_mtime_failure_does_not_reconnect_or_redownload(download_stub, tmp_path, monkeypatch):
    sd, calls = download_stub

    def fail_mtime(*args):
        raise TimeoutError("filesystem unavailable")

    monkeypatch.setattr("pyneolink.sd_card._set_download_mtime", fail_mtime)
    with pytest.raises(TimeoutError, match="filesystem"):
        sd.file({"file_name": "clip.mp4"}).download(tmp_path)
    assert calls == [tmp_path / "clip.mp4"]
    assert calls[0].read_bytes() == b"finalized"


def test_mtime_applies_to_actual_finalized_path(download_stub, tmp_path, monkeypatch):
    sd, _ = download_stub
    started = datetime(2026, 9, 10, 12)

    def different_path(*args, **kwargs):
        path = Path(args[3]).with_suffix(".mp4.bcmedia")
        path.write_bytes(b"raw")
        return path

    monkeypatch.setattr(sd, "_download_once", different_path)
    path = sd.file({"file_name": "clip.mp4", "start_time": started}).download(tmp_path)
    assert path.name == "clip.mp4.bcmedia"
    assert path.stat().st_mtime == pytest.approx(started.timestamp(), rel=0, abs=0.001)


def test_failed_transfer_does_not_change_existing_mtime(download_stub, tmp_path, monkeypatch):
    sd, _ = download_stub
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"existing")
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_001_000_000_000))

    def fail_download(*args, **kwargs):
        raise ValueError("transfer failed")

    monkeypatch.setattr(sd, "_download_once", fail_download)
    with pytest.raises(ValueError, match="transfer failed"):
        sd.file({"file_name": "clip.mp4", "start_time": "2026-09-10T12:00:00"}).download(tmp_path)
    assert path.stat().st_mtime_ns == 1_600_000_001_000_000_000


def test_failed_list_retry_keeps_template_and_sets_mtime(download_stub, tmp_path, monkeypatch):
    sd, calls = download_stub
    download_once = sd._download_once

    def fail_download(*args, **kwargs):
        raise ValueError("transfer failed")

    monkeypatch.setattr(sd, "_download_once", fail_download)
    files = SDFileCollection(sd, [sd.file({"file_name": "clip.mp4", "start_time": "2026-09-10T12:00:00"})])
    template = "{start_time:%Y-%m-%d_%H-%M-%S}_{stem}"
    result = files.download(tmp_path, filename_template=template)
    assert result.failed == 1
    monkeypatch.setattr(sd, "_download_once", download_once)
    retry = result.failed_list().download(tmp_path, filename_template=template)
    assert retry.ok
    assert calls == [tmp_path / "2026-09-10_12-00-00_clip.mp4"]
    assert calls[0].stat().st_mtime == pytest.approx(datetime(2026, 9, 10, 12).timestamp(), rel=0, abs=0.001)
