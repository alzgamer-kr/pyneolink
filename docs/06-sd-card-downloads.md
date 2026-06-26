# SD Card And Downloads

SD-card API живе у `pyneolink/sd_card.py`.

Публічна точка входу:

```python
sd = camera.sd_card()
files = sd.list(start="2026-06-03", end="2026-06-03")
sd.download(files[-1], "downloads", quality="high")
```

## Об'єкти

`SdCardFile` - нормалізований запис про файл:

- `file_name`;
- `path`;
- `size`;
- `start_time`;
- `end_time`;
- `stream_type`;
- `file_type`;
- `channel_id`;
- `raw`.

`raw` зберігає оригінальні поля камери. Download часто потребує саме їх.

## List flow

`SdCard.list()`:

1. нормалізує `start`/`end` у datetime range;
2. пробує `_recorded_days()` через `MSG.DAY_RECORDS`;
3. якщо камера не повернула дні, перебирає всі дні в range;
4. для кожного дня викликає `_list_day_files()`;
5. сортує записи через `_sort_recordings()`;
6. повертає list of dict або list of `SdCardFile`.

## Recorded days

`_recorded_days()` будує `_day_records_range_query()`:

- `msg_id = MSG.DAY_RECORDS`;
- payload містить channel і start/end date range.

Якщо камера повернула `dayType/index`, код перетворює index у конкретну дату.

## Handle discovery

Багато камер не повертають одразу весь список файлів. Тому list flow має два етапи:

1. `_handle_queries()` отримує `handle`;
2. `_handle_detail_queries()` використовує цей handle, щоб читати сторінки.

Практично це виглядає так:

- `handle/mainStream`;
- `files/handle-1`;
- `files/handle-1/page-2`;
- `files/handle-1/page-3`;
- ...

Код продовжує сторінки, доки камера повертає нові `FileInfo`.

## Pagination

`_list_handle_files(..., max_pages=64)` повторює той самий detail query. Камера сама повертає наступну сторінку для активного handle.

Зупинка:

- response не `200`;
- немає `FileInfo`;
- нових файлів не додано;
- досягнуто `max_pages`.

## Filter

`SdCard.filter()` працює вже на отриманому списку:

- фільтр по `start`/`end`;
- substring по `name`;
- exact `file_type`;
- exact `stream_type`.

Це не новий запит до камери, якщо `files` передано явно.

## Download flow

`SdCard.download()`:

1. приводить file до dict;
2. бере `raw` через `_download_raw()`;
3. застосовує `quality` або `stream_type`;
4. генерує temporary playback channel id;
5. формує output path;
6. рахує expected size з `size`, `sizeL`, `sizeH`;
7. перебирає download strategies з `_download_queries()`;
8. пише у `*.part`;
9. перевіряє size;
10. фіналізує файл через `_finalize_download()`.

## Download strategies

Через різні моделі/firmware код пробує кілька способів.

Для high quality (`mainStream`) forced path:

- `download13/full-high/class6482`;
- `download8/full-high/class6482`.

Для generic path:

- `download13/id/class6482`;
- `playback143/range-.../bcmedia`;
- `download8/id/class6482`;
- `replay5/start/bcmedia`;
- інші варіанти `filename`, `name`, `full`;
- fallback з `class6414`.

Це не елегантно, але практично: різні камери приймають різні XML shape/message class.

## Binary download receive loop

`_download_with_query()`:

1. відправляє query через `camera.send()`;
2. приймає багато `Message`;
3. приймає continuation messages, навіть якщо `msg_num` змінюється;
4. якщо extension містить `<binaryData>1</binaryData>`, додає msg_num до `binary_msg_nums`;
5. пише payload у `.part`;
6. шле download keepalive;
7. завершує по XML done, response `201`/`300`, timeout після прогресу або expected size.

Для download важливо, що payload може бути:

- XML metadata;
- Baichuan binary message;
- raw BCMedia tail після invalid magic.

Тому download loop складніший за звичайний `Camera.command()`.

## Finalize

`_finalize_download()`:

- перевіряє expected size, якщо він відомий;
- якщо output `.mp4`, але downloaded file виглядає як BCMedia, викликає `bcmedia_to_mp4()`;
- якщо conversion failed, пробує `extract_embedded_mp4()`;
- якщо і це не допомогло, зберігає raw як `*.mp4.bcmedia` і кидає `ProtocolError`.

## Remove and format

`remove()` поки не підключений:

```python
raise NotImplementedError(...)
```

`format()` існує, але захищений:

- потрібен `confirm=True`;
- потрібен `confirmation_text="FORMAT SD CARD"`;
- тільки після цього надсилається `MSG.HDD_INIT`.

Це свідомий захист від випадкового форматування SD-card.
