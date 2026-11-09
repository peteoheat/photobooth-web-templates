# CLAUDE.md

## Project overview

`photobooth-gallery` is a plugin for [photobooth-app](https://github.com/photobooth-app/photobooth-app) (v9+), ported from
[pibooth-gallery](https://github.com/peteoheat/pibooth-gallery) when moving off pibooth. It builds a per-event static web gallery
(thumbnails -> full image, download QR code, download/share buttons) under `media/gallery/<event>/` and lets the built-in
Synchronizer plugin upload it.

## Layout

| Path | Purpose |
|---|---|
| `photobooth_gallery/photobooth_gallery.py` | The plugin. Class `PhotoboothGallery` (name is derived by photobooth-app from the module name: `photobooth_gallery` -> `PhotoboothGallery`). `__version__` lives here. |
| `photobooth_gallery/config.py` | pydantic-settings config (`BaseConfig`), shown in the admin UI, saved to `config/plugin_photobooth_gallery.json`. |
| `photobooth_gallery/templates/` | `classic.html`, `snowflake.html`: self-contained pages (no external resources, works on offline hotspots). |
| `tests/` | pytest. `conftest.py` stubs the photobooth modules when photobooth-app isn't installed. |

## Key facts about photobooth-app (verified against v9.0.1 source)

- Plugins: pluggy project `"photobooth-app"`; hookspecs in `photobooth/plugins/__init__.py`. Pip plugins load from entry point group
  **`photobooth11`** (the docs say `photobooth.plugins`; the code wins). User plugins: `photobooth-data/plugins/<name>/<name>.py`.
- Cwd is `photobooth-data`. Paths are relative: `media/processed_full/` (photos), `cache/` (app thumbnails), `userdata/`, `config/`.
- `collection_files_added(files, priority_modifier)` only passes paths; the plugin looks up the `Mediaitem` by `processed` path in
  SQLite to get id/type/show_in_gallery/revision. Collage parts have `show_in_gallery=False`. Camera originals also arrive (ignored).
- The QR code on the booth screen is rendered client-side from `GET /api/share/download/{id}` = flattened `get_share_links` hook results.
  We call the same hook and render an SVG with `segno`.
- Synchronizer uploads any file under `./media/` passed to `collection_files_added` (paths must be relative to cwd) and mirrors
  `media/` in regular sync. Our own files come back through our hook; `_is_gallery_file` filters them (avoid loops).
- No plugin API for HTTP routes: local serving inserts Starlette `Mount`s at the front of `photobooth.application.app.router.routes`.
- `utils.media_resizer.resize` uses simplejpeg DCT scaling for JPEGs: output is >= the requested length (1/2, 1/4, 1/8 steps), not <=.
- `PathType` (photobooth-app) has no `cache_ok`, so the first query on `Mediaitem` in a process logs a SAWarning. Upstream, harmless.

## Windows file-replace races (found running the real app on Windows)

- Writer: `os.replace` onto `thumbs.json` fails with PermissionError while a reader has it open -> `_replace_with_retry`.
- Reader: Starlette `FileResponse` stats then streams; a replace in between gives "Response content longer than Content-Length",
  and opening during the replace gives PermissionError -> `GalleryFiles` serves `.json` from one read, retries, else 503.
- Tests that poll the manifest from another thread must tolerate read errors (`wait_for_items`).

## Testing against the real photobooth-app

Clone photobooth-app, `uv sync`, `uv pip install segno` into its `.venv`, then run pytest with that interpreter **from a scratch
directory** (importing `photobooth` creates `media/`, `config/`, `database/`... in the cwd). `conftest.STUBBED` is False then.
End-to-end: scratch photobooth-data with `plugins/photobooth_gallery/` + `config/plugin_photobooth_gallery.json`
(`{"common": {"enabled": true}}`), `python -m photobooth --port 8010`, capture via `GET /api/actions/image/0`,
delete via `DELETE /api/mediacollection/{id}`, filter via `PATCH /api/filter/{id}?filter=FilterPilgram2._1977`.

## Conventions

- Hooks only enqueue; one worker thread does all file work, so manifest writes are serialized. Never let exceptions escape hooks.
- Atomic writes (temp + `os.replace`, chmod 644). Corrupt manifest moved to `.bak`.
- Manifest stays compatible with pibooth-gallery templates (`thumb`, `full`, `filename`, `qrcode`, list newest first).
- Integration points (`lookup_item`, `list_items`, `make_thumbnail`, `share_link`, `publish`) are methods so tests can swap them.
- Tests: `pytest` (a scratch venv with pytest, pillow, pydantic-settings, pluggy, segno, starlette, httpx is enough).
- Dev machine is Windows, target is Raspberry Pi/Linux; keep LF line endings. Commit/push only when asked.
