# photobooth-gallery

A plugin for [photobooth-app](https://photobooth-app.org) that builds a **per-event web gallery**:
a page of thumbnails that open the full photo, each with the **download QR code** the
photobooth already shows on screen, plus Download / Share buttons.

It is the photobooth-app successor of [pibooth-gallery](https://github.com/peteoheat/pibooth-gallery)
and keeps the same idea: generate static files next to the photos and let the uploader publish them.

* Thumbnails, QR codes, a `thumbs.json` manifest and `index.html` are generated in a background thread for every new photo, so the booth never waits.
* The QR code encodes **the same share link photobooth-app uses for its own QR code** (from the Synchronizer plugin), so guests get the same download experience from the gallery as from the booth screen.
* New files are handed to the built-in **Synchronizer** plugin and uploaded immediately with the photo, so the online gallery updates live (an open page picks up new photos every few seconds).
* The gallery is also served by the photobooth itself at `http://<photobooth>:8000/media/gallery/<event>/`, handy with a local hotspot.
* Each event gets its own folder; change the event name for the next party and a new, empty gallery starts. Old galleries are kept.
* Deleting a photo in photobooth-app removes it from the gallery (and from the web host); applying a filter refreshes its thumbnail.
* Two built-in designs (`classic`, light/dark automatic, and `snowflake`, the seasonal theme) or your own template. Templates written for pibooth-gallery that read `thumbs.json` keep working.

## How it works

```
photobooth-data/
  media/processed_full/IMG.jpg                 <- photobooth-app
  media/gallery/<event>/index.html             <- gallery page
  media/gallery/<event>/thumbs.json            <- manifest, newest first
  media/gallery/<event>/thumbs/IMG_thumb.jpg
  media/gallery/<event>/qrcodes/IMG_qrcode.svg
```

The page links full images relatively (`../../processed_full/IMG.jpg`). The Synchronizer mirrors
`media/` to `<remote subdir>/media/`, so the gallery works unchanged on the web host at
`https://<your host>/<subdir>/media/gallery/<event>/`.

Hooks used (see `photobooth/plugins/__init__.py` in photobooth-app):

| Hook | What the plugin does |
|---|---|
| `start` / `stop` | start/stop the worker thread, write the page, tidy the manifest, mount the local web routes |
| `collection_files_added` | add the item (only items shown in the photobooth gallery, of the configured media types) |
| `collection_files_updated` | regenerate the thumbnail (e.g. after a filter), keep its place |
| `collection_files_deleted` | remove the entry, its thumbnail and QR code, locally and on the remote |
| `get_share_links` (called) | fetch the link photobooth-app puts in its QR code |
| `collection_files_added` / `_deleted` (called) | hand gallery files to the Synchronizer for upload |
| `get_stats` | status card on the admin dashboard |

## Installation

Requires photobooth-app v9 or newer.

**Option A: install as a package** into the same Python environment photobooth-app runs in.
photobooth-app finds it through its `photobooth11` entry point group.

```bash
# if photobooth-app was installed with pipx:
pipx inject photobooth-app git+https://github.com/peteoheat/photobooth-gallery.git
# or with uv tool:
uv tool install photobooth-app --with git+https://github.com/peteoheat/photobooth-gallery.git
# or plain pip inside photobooth-app's virtual environment:
pip install git+https://github.com/peteoheat/photobooth-gallery.git
```

**Option B: user plugin folder.** Copy the `photobooth_gallery` folder into
`photobooth-data/plugins/`, so you have `photobooth-data/plugins/photobooth_gallery/photobooth_gallery.py`,
and install the QR code library into photobooth-app's environment (e.g. `pipx inject photobooth-app segno`).
Without `segno` the gallery still works, but without QR codes (a warning is logged).

Restart photobooth-app. "Event Gallery" now appears in Admin > Config under plugins.

> The photobooth-app documentation names the entry point group `photobooth.plugins`; the
> app's code (v9) actually reads `photobooth11`, which is what this package registers.

## Setup

1. In **Admin > Config > Synchronizer**, set up your remote and share link as usual
   (`manual_public_link`, optionally with the share page). That link becomes the QR code.
   Keep *immediate sync* enabled so the gallery updates live.
2. In **Admin > Config > Event Gallery**: enable it, set the event title, pick a theme, save, and reload services.
3. Take a photo, then open `http://<photobooth>:8000/media/gallery/<event>/` or the same path on your web host.

## Publishing to your web host with the Synchronizer

There is nothing gallery-specific to configure in the Synchronizer: everything the gallery needs
lives under `media/`, which is exactly what the Synchronizer uploads.

| Local (photobooth-data) | On the remote |
|---|---|
| `media/processed_full/IMG.jpg` | `<subdir>/media/processed_full/IMG.jpg` |
| `media/gallery/<event>/index.html`, `thumbs.json` | `<subdir>/media/gallery/<event>/...` |
| `media/gallery/<event>/thumbs/`, `qrcodes/` | `<subdir>/media/gallery/<event>/...` |
| *(Synchronizer's own share page)* | `<subdir>/index.html` |

Files reach the remote in two ways; either one alone is enough:

* **Immediate sync** (live): the gallery hands every new or changed file to the Synchronizer right
  after the photo, and removes deleted ones. The page and manifest written at startup are sent again
  with the first photo, and at the latest 15 s after startup, because the Synchronizer starts after this plugin.
* **Regular sync** (every *full_sync_interval* minutes, and once at startup): mirrors all of `media/`,
  so it also repairs anything missed, for example uploads made while offline.

In **Admin > Config > Synchronizer**, for your remote:

1. `enabled`, and `enable_immediate_sync` on for a live gallery; keep `enable_regular_sync` on as the safety net.
2. `name` / `subdir`: the folder you serve on the web, e.g. `subdir = event123` served as `https://example.com/event123/`.
   **The folder must already exist on the remote**; otherwise immediate sync skips the remote ("not available").
3. Share link (the QR code): `shareconfig.enabled`, and `manual_public_link` pointing at the uploaded photo,
   e.g. `https://example.com/event123/media/processed_full/{filename}` (optionally through the share page).
4. Mind `copy_only_mode`: when on, deletions are not propagated (removed photos keep their thumbnail and QR
   file on the host, although they disappear from the gallery page). When off, the Synchronizer owns
   `<subdir>/media/` and deletes anything there that isn't on the photobooth, so put extra files
   (e.g. `robots.txt`) outside `media/`.

The gallery is then at `https://example.com/event123/media/gallery/<event>/`. The web server must serve
`index.html` for a directory URL (most do; on S3 enable static website hosting, or link to `.../index.html`).

Check the online copy is complete: `index.html`, `thumbs.json`, `thumbs/` and `qrcodes/` should be in
`<subdir>/media/gallery/<event>/`, with the photos in `<subdir>/media/processed_full/`.

> **Windows booths:** photobooth-app v9.0.1's immediate sync fails on Windows for every file, photos
> included (it passes rclone `media\processed_full\...` with backslashes; rclone.log shows
> `operations/copyfile: object not found`). Only regular sync works there. Raspberry Pi / Linux is not affected.

## Configuration

Stored in `photobooth-data/config/plugin_photobooth_gallery.json`, edited in the admin UI.

| Group | Option | Default | Description |
|---|---|---|---|
| common | `enabled` | `false` | Turn the gallery on. |
| common | `event_title` | `Our Event` | Page title. |
| common | `event_folder` | *(empty)* | Folder below `media/gallery/`; empty = derived from the title. Change for each event. |
| common | `subtitle` | thank-you text | Line under the title. |
| common | `theme` | `classic` | `classic`, `snowflake` or `custom`. |
| common | `custom_template` | *(empty)* | Your HTML template, e.g. `userdata/my_gallery.html`. |
| common | `poll_seconds` | `10` | How often an open page checks for new photos; 0 = off. |
| media | `media_types` | image, collage, animation | Also `video`, `multicamera` (shown as looping video thumbnails). |
| media | `thumbnail_length` | `400` | Minimum longest side of thumbnails in pixels (photobooth-app's fast JPEG scaler rounds up, to at most about twice this). |
| media | `backfill_on_start` | `false` | Add everything already in the collection on startup. |
| qrcode | `enabled` | `true` | Show the download QR code. |
| qrcode | `share_link_index` | `0` | Which share link to use when several are configured. |
| qrcode | `fallback_url` | *(empty)* | Used when no share link exists; `{filename}` and `{identifier}` are replaced. |
| publishing | `sync_via_synchronizer` | `true` | Upload gallery files immediately via the Synchronizer. |
| publishing | `serve_locally` | `true` | Serve `/media/gallery/` and `/media/processed_full/` from the photobooth. |

### Custom templates

Any HTML file. These placeholders are replaced: `{{TITLE}}`, `{{SUBTITLE}}` (HTML-escaped),
`{{MANIFEST}}` (`thumbs.json`) and `{{POLL_SECONDS}}`. The page fetches the manifest relative to itself.
Start by copying `photobooth_gallery/templates/classic.html`.

### Manifest (`thumbs.json`)

A JSON list, newest first. The first four keys are the pibooth-gallery contract.

```json
[
  {
    "thumb": "thumbs/IMG_thumb.jpg",
    "full": "../../processed_full/IMG.jpg",
    "filename": "IMG.jpg",
    "qrcode": "qrcodes/IMG_qrcode.svg",
    "share_url": "https://example.com/sharepage/#/?url=...",
    "id": "5f0c...",
    "type": "image",
    "created": "2026-11-09T20:15:00",
    "revision": 0
  }
]
```

`thumb` gets `?v=<revision>` after a filter is applied, for cache busting.

## Notes

* Local serving: photobooth-app has no plugin API for web routes, so the plugin inserts two static
  mounts (`/media/gallery`, `/media/processed_full`) at the front of the running app's routes,
  and removes them on stop. Turn off `serve_locally` if that is ever a problem.
* A one-time `SAWarning: TypeDecorator PathType() will not produce a cache key` in the log comes from
  photobooth-app's database column type (the first query on its media table triggers it) and is harmless.
* Protecting the online gallery (e.g. with [gallery-protect](https://github.com/peteoheat/gallery-protect)) works as before, since it is plain static files.
* The page has `noindex` set. Add a `robots.txt` on your host as well if you want.

## Development

```bash
pip install -e .[test]   # photobooth-app itself is optional for the tests
pytest
```

Without photobooth-app installed the tests use a small stand-in for the photobooth modules
(`tests/conftest.py`) with the same hook specs; with photobooth-app installed they run against it.

## License

MIT, see [LICENSE](LICENSE).
