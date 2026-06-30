# SD Card And Downloads

The SD-card API lives in `pyneolink/sd_card.py`.

Public entry point:

```python
sd = camera.sd_card()
files = sd.files(start="2026-06-03", end="2026-06-03")
files[-1].download("downloads", quality="high", rewrite_exists=False)

for video in sd.files(start="2026-06-03", end="2026-06-03", name=".mp4"):
    print(video.info()["file_name"])
```

## Objects

`SdCardFile` is a normalized recording/file metadata item:

- `file_name`;
- `path`;
- `size`;
- `start_time`;
- `end_time`;
- `stream_type`;
- `file_type`;
- `channel_id`;
- `raw`.

`raw` keeps the original camera fields. Download often needs those fields.

`SDFile` wraps one recording and exposes actions for that recording:

- `info()`;
- `download(...)`;
- `preview(...)`.

`SdCard.files()` returns `SDFile` objects directly and can apply a simple
`name` substring filter, for example `sd.files(name=".mp4")`. `SdCard.list()`
remains available when plain dictionaries or `SdCardFile` metadata objects are
more convenient.

## List Flow

`SdCard.list()`:

1. normalizes `start`/`end` into a datetime range;
2. tries `_recorded_days()` through `MSG.DAY_RECORDS`;
3. if the camera does not return recorded days, iterates through every day in the range;
4. calls `_list_day_files()` for each day;
5. sorts recordings through `_sort_recordings()`;
6. returns either a list of dicts or a list of `SdCardFile` objects.

## Recorded Days

`_recorded_days()` builds `_day_records_range_query()`:

- `msg_id = MSG.DAY_RECORDS`;
- payload contains channel and start/end date range.

If the camera returns `dayType/index`, the code converts the index into a concrete date.

## Handle Discovery

Many cameras do not return the full file list immediately. The list flow therefore has two stages:

1. `_handle_queries()` gets a `handle`;
2. `_handle_detail_queries()` uses that handle to read pages.

In practice this looks like:

- `handle/mainStream`;
- `files/handle-1`;
- `files/handle-1/page-2`;
- `files/handle-1/page-3`;
- ...

The code continues reading pages while the camera returns new `FileInfo` entries.

## Pagination

`_list_handle_files(..., max_pages=64)` repeats the same detail query. The camera advances the active handle internally and returns the next page.

Pagination stops when:

- response is not `200`;
- no `FileInfo` is returned;
- no new files are added;
- `max_pages` is reached.

## Filter

`SdCard.filter()` works on an already loaded list:

- `start`/`end` filter;
- substring match on `name`;
- exact `file_type`;
- exact `stream_type`.

This is not a new camera request when `files` is passed explicitly.

## Download Flow

`SDFile.download()`:

1. converts the wrapped file metadata to a dict;
2. extracts `raw` through `_download_raw()`;
3. applies `quality` or `stream_type`;
4. builds the output path;
5. calculates expected size from `size`, `sizeL`, and `sizeH`;
6. if `rewrite_exists=False` and the local file has the same size, returns it without downloading;
7. generates a temporary playback channel id;
8. tries download strategies from `_download_queries()`;
9. writes to `*.part`;
10. validates size;
11. on `DownloadSizeMismatch` or `TimeoutError`, waits 5 seconds, reconnects, and restarts the file download according to `reconnect_retries`;
12. finalizes the file through `_finalize_download()`.

Useful download options:

- `reconnect_retries=3` limits reconnect attempts after one interrupted download; after a successful reconnect, the file download is started again.
- `rewrite_exists=False` skips a local file when its size already matches the camera file.
- For `.mp4` outputs, any non-empty final `.mp4` is treated as complete because PyNeolink only creates the final file after successful finalization; interrupted downloads remain as `.part` files.
- `progress=True` prints skip/retry/download progress messages.

If reconnect fails after the configured number of attempts, `CameraConnectionError` is raised. This lets caller code stop a batch job cleanly when the camera is unavailable.

## Preview Playback

`SDFile.preview()` opens the camera preview/playback stream for one recording
and caches the embedded MP4 to a local file. The cache is file based on purpose:
players and HTTP clients can read a growing file efficiently, while PyNeolink
does not need to keep large video data in memory.

Basic cache use:

```python
with file.preview(cleanup=True, progress=True) as preview:
    preview.wait_ready(timeout=15)
    print(preview.path)
```

For players such as VLC, serve the preview cache over HTTP:

```python
with file.preview(cleanup=True, progress=True) as preview:
    with preview.serve(port=8560) as server:
        print(server.url)
        input("Press Enter to stop preview...")
```

The HTTP reader starts from the beginning of the cached MP4. If it reaches the
current end of the cache while the camera is still sending data, it waits for
more bytes instead of closing the response. When the last connected player
disconnects, the preview cache is closed and removed when `cleanup=True`.
When `cache` is omitted, preview files are stored under
`.tmp/pyneolink-preview-cache/`, which is ignored by Git.

## Download Strategies

Different camera models and firmware versions accept different XML shapes and message classes, so PyNeolink tries several strategies.

For forced high quality (`mainStream`):

- `download13/full-high/class6482`;
- `download8/full-high/class6482`.

For the generic path:

- `download13/id/class6482`;
- `playback143/range-.../bcmedia`;
- `download8/id/class6482`;
- `replay5/start/bcmedia`;
- other `filename`, `name`, and `full` variants;
- fallback variants with `class6414`.

This is not elegant, but it is practical: different cameras accept different request shapes.

## Binary Download Receive Loop

`_download_with_query()`:

1. sends a query through `camera.send()`;
2. receives many `Message` objects;
3. accepts continuation messages, even when `msg_num` changes;
4. if the extension contains `<binaryData>1</binaryData>`, adds msg numbers to `binary_msg_nums`;
5. writes payload to `.part`;
6. sends download keepalive;
7. finishes on XML done, response `201`/`300`, timeout after progress, or expected size.

For downloads, payload may be:

- XML metadata;
- a Baichuan binary message;
- raw BCMedia tail after invalid magic.

This is why the download loop is more complex than normal `Camera.command()`.

## Finalize

`_finalize_download()`:

- validates expected size when known;
- if output is `.mp4` but the downloaded file looks like BCMedia, calls `bcmedia_to_mp4()`;
- if conversion fails, tries `extract_embedded_mp4()`;
- if that also fails, saves the raw stream as `*.mp4.bcmedia` and raises `ProtocolError`.

## Remove And Format

`remove()` is not wired yet:

```python
raise NotImplementedError(...)
```

`format()` exists, but it is guarded:

- requires `confirm=True`;
- requires `confirmation_text="FORMAT SD CARD"`;
- only then sends `MSG.HDD_INIT`.

This is an intentional guard against accidental SD-card formatting.
