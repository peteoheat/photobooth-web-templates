"""
photobooth-gallery: per-event web gallery plugin for photobooth-app.

For every item that photobooth-app adds to its collection (and shows in its own gallery) the
plugin, in a background thread:

1. creates a thumbnail,
2. renders the download QR code as an SVG, using the same share link the photobooth shows as
   QR code on screen (``get_share_links`` hook, normally answered by the Synchronizer plugin),
3. adds an entry to the event's ``thumbs.json`` manifest (newest first),
4. keeps the gallery page ``index.html`` beside it,
5. hands the new files to the Synchronizer plugin (``collection_files_added`` hook) so they are
   uploaded straight away, exactly like the photos themselves.

Layout (all paths relative to the photobooth-data working directory)::

    media/processed_full/<photo>.jpg            # written by photobooth-app
    media/gallery/<event>/index.html            # gallery page
    media/gallery/<event>/thumbs.json           # manifest
    media/gallery/<event>/thumbs/<photo>_thumb.jpg
    media/gallery/<event>/qrcodes/<photo>_qrcode.svg

The page refers to full images relatively (``../../processed_full/<photo>.jpg``), so the same
folder structure works on the photobooth (served at ``/media/gallery/<event>/``) and on any web
host the Synchronizer mirrors ``media/`` to.

Error policy: as with pibooth-gallery, nothing here may break the photobooth. Hooks only queue
work; the worker logs failures and carries on.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import UUID

from photobooth import MEDIA_PATH, PATH_PROCESSED
from photobooth.models.genericstats import GenericStats, SubStats
from photobooth.plugins import hookimpl, pm
from photobooth.plugins.base_plugin import BasePlugin

from .config import GalleryConfig

__version__ = "1.1.0"

logger = logging.getLogger(__name__)

GALLERY_ROOT = Path(MEDIA_PATH, "gallery")
MANIFEST_NAME = "thumbs.json"
PAGE_NAME = "index.html"
THUMB_DIR = "thumbs"
QR_DIR = "qrcodes"
TEMPLATE_DIR = Path(__file__).parent / "templates"

URL_GALLERY = "/media/gallery"
URL_PROCESSED = "/media/processed_full"
MOUNT_NAMES = ("photobooth_gallery_pages", "photobooth_gallery_full")

# Synchronizer priority is 10 + modifier, lower runs first. The photo itself uses -1, so gallery
# files follow right after it, ahead of the camera originals (+2).
SYNC_PRIORITY_MODIFIER = 1

# The Synchronizer normally starts after this plugin, so what we hand it during startup is dropped.
# Startup files are therefore sent again with the next change, and at the latest after this delay.
REPUBLISH_DELAY_S = 15.0


@dataclass(frozen=True)
class ItemInfo:
    """The parts of a photobooth-app ``Mediaitem`` the gallery needs, detached from the DB session."""

    id: UUID
    media_type: str
    processed: Path
    show_in_gallery: bool
    revision: int
    created_at: datetime | None

    @classmethod
    def from_model(cls, item) -> ItemInfo:
        return cls(
            id=item.id,
            media_type=item.media_type,
            processed=Path(item.processed),
            show_in_gallery=bool(item.show_in_gallery),
            revision=int(item.revision or 0),
            created_at=item.created_at,
        )


@dataclass(frozen=True)
class _Task:
    kind: str  # "init" | "add" | "update" | "delete" | "republish"
    path: Path | None = None


# --------------------------------------------------------------------------- #
# Small helpers (no photobooth state)
# --------------------------------------------------------------------------- #


def slugify(text: str, default: str = "event") -> str:
    """Folder-safe name: ASCII letters, digits, '.', '_' and '-' only. Never escapes the gallery root."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_text).strip("-.")
    return slug or default


def atomic_write(path: Path, data: str | bytes) -> None:
    """Write via temp file + rename, so neither the web server nor rclone sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    encoding = None if isinstance(data, bytes) else "utf-8"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, mode, encoding=encoding) as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; keep it readable for web servers
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _replace_with_retry(src: Path, dst: Path, attempts: int = 10) -> None:
    """``os.replace`` that tolerates Windows refusing to replace a file someone has open.

    On Windows the gallery's own web server (or a virus scanner, OneDrive, ...) briefly holding
    ``thumbs.json`` open makes the rename fail with PermissionError. Linux never does this.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def load_manifest(path: Path) -> list[dict]:
    """Load the manifest list. A corrupt file is moved aside to ``.bak`` rather than overwritten."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        reason = "not a JSON list"
    except Exception as exc:
        reason = str(exc)

    backup = path.with_name(path.name + ".bak")
    try:
        os.replace(path, backup)
        logger.warning(f"gallery: {path} unreadable ({reason}); moved to {backup}, starting a new manifest")
    except OSError as exc:
        logger.warning(f"gallery: {path} unreadable ({reason}) and could not be backed up ({exc})")
    return []


def relative_url(target: Path, base_dir: Path) -> str:
    """URL of ``target`` relative to the page in ``base_dir``, percent-encoded per segment."""
    rel = Path(os.path.relpath(target, base_dir)).as_posix()
    return "/".join(quote(part) for part in rel.split("/"))


def sort_newest_first(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: str(e.get("created") or ""), reverse=True)


# --------------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------------- #


class Gallery(BasePlugin[GalleryConfig]):
    def __init__(self):
        super().__init__()
        self._config: GalleryConfig = GalleryConfig()

        self._queue: queue.Queue[_Task | None] | None = None
        self._worker: threading.Thread | None = None
        self._gallery_dir: Path | None = None
        self._item_count: int = 0
        self._last_error: str | None = None
        self._qr_unavailable_warned = False
        self._republish_timer: threading.Timer | None = None
        # startup files that the Synchronizer may not have received yet (see REPUBLISH_DELAY_S)
        self._carry_added: list[Path] = []
        self._carry_deleted: list[Path] = []

    def __str__(self):
        return "Gallery"

    # ------------------------------------------------------------------ hooks

    @hookimpl
    def start(self):
        self.stop()  # idempotent: never leave a second worker or duplicate mounts behind

        if not self._config.common.enabled:
            logger.info("gallery: plugin disabled in config")
            return

        self._gallery_dir = self.gallery_dir()
        self._last_error = None
        self._carry_added, self._carry_deleted = [], []
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name="photobooth-gallery", daemon=True)
        self._worker.start()
        self._queue.put(_Task("init"))
        self._republish_timer = threading.Timer(REPUBLISH_DELAY_S, self._queue.put, args=(_Task("republish"),))
        self._republish_timer.daemon = True
        self._republish_timer.start()

        if self._config.publishing.serve_locally:
            self._mount_local()

        logger.info(f"gallery: event '{self._config.common.event_title}' in {self._gallery_dir}, local URL {URL_GALLERY}/{self._gallery_dir.name}/")

    @hookimpl
    def stop(self):
        self._unmount_local()

        if self._republish_timer:
            self._republish_timer.cancel()
            self._republish_timer = None

        if self._queue and self._worker:
            self._queue.put(None)
            self._worker.join(timeout=10)
            if self._worker.is_alive():
                logger.warning("gallery: worker did not finish within 10s")
        self._queue = None
        self._worker = None

    @hookimpl
    def get_stats(self) -> GenericStats | None:
        if not self._config.common.enabled or not self._gallery_dir:
            return None
        stats = GenericStats(id="hook-plugin-photobooth-gallery", name="Gallery")
        stats.stats.append(SubStats("Event", self._config.common.event_title))
        stats.stats.append(SubStats("Items in gallery", self._item_count))
        stats.stats.append(SubStats("Pending", self._queue.qsize() if self._queue else 0))
        stats.stats.append(SubStats("Folder", self._gallery_dir.as_posix()))
        if self._config.publishing.serve_locally:
            stats.stats.append(SubStats("Local URL", f"{URL_GALLERY}/{self._gallery_dir.name}/"))
        stats.stats.append(SubStats("Last error", self._last_error))
        return stats

    @hookimpl
    def collection_files_added(self, files: list[Path], priority_modifier: int):
        self._enqueue("add", files)

    @hookimpl
    def collection_files_updated(self, files: list[Path]):
        self._enqueue("update", files)

    @hookimpl
    def collection_files_deleted(self, files: list[Path]):
        self._enqueue("delete", files)

    # ------------------------------------------------------------------ queue

    def _enqueue(self, kind: str, files: list[Path]) -> None:
        if self._queue is None:
            return
        for file in files:
            # our own files come back through this hook when we hand them to the synchronizer
            if not self._is_gallery_file(file):
                self._queue.put(_Task(kind, Path(file)))

    @staticmethod
    def _is_gallery_file(file: Path) -> bool:
        try:
            return Path(file).resolve().is_relative_to(GALLERY_ROOT.resolve())
        except (OSError, ValueError):
            return False

    def _worker_loop(self) -> None:
        assert self._queue is not None
        q = self._queue
        while True:
            task = q.get()
            if task is None:
                return
            try:
                self.process(task)
            except Exception as exc:
                self._last_error = f"{task.kind} {task.path or ''}: {exc}"
                logger.exception(f"gallery: {task.kind} failed for {task.path}")

    def process(self, task: _Task) -> None:
        if task.kind == "init":
            self.initialize()
        elif task.kind in ("add", "update") and task.path is not None:
            self.add_or_update(task.path)
        elif task.kind == "delete" and task.path is not None:
            self.remove(task.path)
        elif task.kind == "republish":
            self.publish([])  # sends whatever startup files are still carried

    # ------------------------------------------------------------------ gallery work

    def gallery_dir(self) -> Path:
        common = self._config.common
        return GALLERY_ROOT / slugify(common.event_folder or common.event_title)

    @property
    def _dir(self) -> Path:
        return self._gallery_dir or self.gallery_dir()

    @property
    def _manifest_path(self) -> Path:
        return self._dir / MANIFEST_NAME

    def initialize(self) -> None:
        """Write the page, drop entries whose photo is gone, optionally backfill, then publish."""
        changed: list[Path] = []
        if self.write_page():
            changed.append(self._dir / PAGE_NAME)

        entries = load_manifest(self._manifest_path)
        kept, removed_files = [], []
        for entry in entries:
            full = entry.get("full")
            if full and not (self._dir / unquote(full)).is_file():
                logger.info(f"gallery: {entry.get('filename')} no longer exists, removing it from the gallery")
                removed_files.extend(self._entry_files(entry))
            else:
                kept.append(entry)

        if self._config.media.backfill_on_start:
            known = {e.get("filename") for e in kept}
            for info in self.list_items():
                if info.processed.name not in known and self._wanted(info):
                    try:
                        entry, files = self._build_entry(info)
                    except Exception as exc:
                        logger.warning(f"gallery: backfill of {info.processed} failed: {exc}")
                        continue
                    kept.append(entry)
                    changed.extend(files)

        if not self._manifest_path.exists() or len(kept) != len(entries) or changed:
            self._write_manifest(kept)
            changed.append(self._manifest_path)
        else:
            self._item_count = len(kept)

        self._delete_local(removed_files)
        self.publish(changed, removed_files)

        # The Synchronizer has usually not started yet and dropped that. Always resend the page and the
        # manifest (even unchanged: an earlier session's upload may have been dropped the same way).
        self._carry_added = list(dict.fromkeys([*changed, self._dir / PAGE_NAME, self._manifest_path]))
        self._carry_deleted = removed_files

    def add_or_update(self, path: Path) -> None:
        info = self.lookup_item(path)
        if info is None:
            logger.debug(f"gallery: {path} is not a collection item, ignored")
            return

        entries = load_manifest(self._manifest_path)
        existing = [e for e in entries if e.get("filename") == info.processed.name]
        if not self._wanted(info):
            if existing:  # e.g. media type filter changed; keep the gallery consistent
                self.remove(path)
            return

        entry, files = self._build_entry(info)
        entries = [e for e in entries if e.get("filename") != info.processed.name]
        entries.append(entry)
        self._write_manifest(entries)
        self.publish([*files, self._manifest_path])
        logger.info(f"gallery: {'updated' if existing else 'added'} {info.processed.name} ({self._item_count} items)")

    def remove(self, path: Path) -> None:
        name = Path(path).name
        entries = load_manifest(self._manifest_path)
        gone = [e for e in entries if e.get("filename") == name]
        if not gone:
            return
        self._write_manifest([e for e in entries if e.get("filename") != name])
        files = [f for e in gone for f in self._entry_files(e)]
        self._delete_local(files)
        self.publish([self._manifest_path], files)
        logger.info(f"gallery: removed {name} ({self._item_count} items)")

    def _wanted(self, info: ItemInfo) -> bool:
        return info.show_in_gallery and info.media_type in self._config.media.media_types

    def _build_entry(self, info: ItemInfo) -> tuple[dict, list[Path]]:
        """Create thumbnail and QR code for one item. Returns the manifest entry and files written."""
        stem, suffix = info.processed.stem, info.processed.suffix
        files: list[Path] = []

        thumb = self._dir / THUMB_DIR / f"{stem}_thumb{suffix}"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        self.make_thumbnail(info.processed, thumb, self._config.media.thumbnail_length)
        files.append(thumb)

        version = f"?v={info.revision}" if info.revision else ""
        entry = {
            "id": str(info.id),
            "type": info.media_type,
            "filename": info.processed.name,
            "full": relative_url(info.processed, self._dir),
            "thumb": relative_url(thumb, self._dir) + version,
            "created": info.created_at.isoformat() if info.created_at else datetime.now().isoformat(),
            "revision": info.revision,
        }

        if self._config.qrcode.enabled:
            url = self.share_link(info)
            if url:
                entry["share_url"] = url
                qr = self._dir / QR_DIR / f"{stem}_qrcode.svg"
                if self.make_qrcode(url, qr):
                    entry["qrcode"] = relative_url(qr, self._dir)
                    files.append(qr)

        return entry, files

    def _entry_files(self, entry: dict) -> list[Path]:
        files = []
        for key in ("thumb", "qrcode"):
            value = entry.get(key)
            if value:
                candidate = (self._dir / unquote(value.split("?", 1)[0])).resolve()
                if candidate.is_relative_to(self._dir.resolve()):  # never touch the photos themselves
                    files.append(self._dir / candidate.relative_to(self._dir.resolve()))
        return files

    @staticmethod
    def _delete_local(files: list[Path]) -> None:
        for file in files:
            try:
                file.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"gallery: could not delete {file}: {exc}")

    def _write_manifest(self, entries: list[dict]) -> None:
        entries = sort_newest_first(entries)
        atomic_write(self._manifest_path, json.dumps(entries, indent=2))
        self._item_count = len(entries)

    def write_page(self) -> bool:
        """Render the gallery page. Returns True when the file changed."""
        page = self._dir / PAGE_NAME
        text = self.render_template()
        if page.is_file() and page.read_text(encoding="utf-8") == text:
            return False
        atomic_write(page, text)
        return True

    def render_template(self) -> str:
        common = self._config.common
        template = TEMPLATE_DIR / "classic.html"
        if common.theme == "snowflake":
            template = TEMPLATE_DIR / "snowflake.html"
        elif common.theme == "custom":
            custom = Path(common.custom_template).expanduser()
            if common.custom_template and custom.is_file():
                template = custom
            else:
                logger.warning(f"gallery: custom template '{common.custom_template}' not found, using the classic theme")

        replacements = {
            "{{TITLE}}": html.escape(common.event_title),
            "{{SUBTITLE}}": html.escape(common.subtitle),
            "{{MANIFEST}}": MANIFEST_NAME,
            "{{POLL_SECONDS}}": str(int(common.poll_seconds)),
        }
        text = template.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text

    # ------------------------------------------------------------------ photobooth-app integration
    # Kept as small methods so tests can replace them without a running photobooth-app.

    def lookup_item(self, path: Path) -> ItemInfo | None:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from photobooth.database.database import engine
        from photobooth.database.models import Mediaitem

        with Session(engine) as session:
            item = session.scalars(select(Mediaitem).where(Mediaitem.processed == Path(path))).first()
            return ItemInfo.from_model(item) if item else None

    def list_items(self) -> list[ItemInfo]:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from photobooth.database.database import engine
        from photobooth.database.models import Mediaitem

        with Session(engine) as session:
            items = session.scalars(select(Mediaitem).order_by(Mediaitem.rowid)).all()
            return [ItemInfo.from_model(item) for item in items]

    def make_thumbnail(self, source: Path, target: Path, length: int) -> None:
        from photobooth.utils.media_resizer import resize  # handles jpg, gif, mp4, webp, avif

        resize(filepath_in=source, filepath_out=target, scaled_min_length=length)

    def share_link(self, info: ItemInfo) -> str | None:
        """The link photobooth-app encodes in its own QR code for this item."""
        links: list[str] = []
        try:
            results = pm.hook.get_share_links(filepath_local=info.processed, identifier=info.id)
            links = [link for result in results for link in (result or []) if link]
        except Exception as exc:
            logger.warning(f"gallery: could not get share links for {info.processed.name}: {exc}")

        index = self._config.qrcode.share_link_index
        if links:
            if index >= len(links):
                logger.warning(f"gallery: share_link_index {index} but only {len(links)} share link(s); using the first")
                index = 0
            return links[index]

        fallback = self._config.qrcode.fallback_url
        if fallback:
            return fallback.replace("{filename}", info.processed.name).replace("{identifier}", str(info.id))
        logger.debug(f"gallery: no share link for {info.processed.name}; no QR code")
        return None

    def make_qrcode(self, url: str, target: Path) -> bool:
        try:
            import segno
        except ImportError:
            if not self._qr_unavailable_warned:
                logger.warning("gallery: python package 'segno' is not installed; QR codes are skipped (pip install segno)")
                self._qr_unavailable_warned = True
            return False

        buffer = io.BytesIO()
        segno.make_qr(url, error="m").save(buffer, kind="svg", scale=10, border=2, dark="#000", light="#fff", xmldecl=False)
        atomic_write(target, buffer.getvalue())
        return True

    def publish(self, added: list[Path], deleted: list[Path] | None = None) -> None:
        """Hand gallery files to the Synchronizer (and any other collection plugin) for upload.

        Files carried over from startup go along with the first call. rclone skips files that are
        already identical on the remote, so resending costs next to nothing.
        """
        if not self._config.publishing.sync_via_synchronizer:
            return
        added = [f for f in dict.fromkeys([*self._carry_added, *added]) if f.is_file()]
        deleted = [f for f in dict.fromkeys([*self._carry_deleted, *(deleted or [])]) if not f.exists()]
        self._carry_added, self._carry_deleted = [], []
        try:
            if deleted:
                pm.hook.collection_files_deleted(files=list(deleted))
            if added:
                pm.hook.collection_files_added(files=list(dict.fromkeys(added)), priority_modifier=SYNC_PRIORITY_MODIFIER)
        except Exception as exc:
            logger.warning(f"gallery: handing files to the synchronizer failed: {exc}")

    def _mount_local(self) -> None:
        """Serve media/gallery and media/processed_full from photobooth-app's own web server.

        photobooth-app has no plugin API for routes, so the mounts are inserted at the front of the
        running FastAPI app's route list (ahead of /media/{dimension}/{id} and the SPA catch-all).
        """
        app = getattr(sys.modules.get("photobooth.application"), "app", None)
        if app is None:
            logger.info("gallery: photobooth web app not loaded; not serving the gallery locally")
            return
        try:
            from starlette.responses import Response
            from starlette.routing import Mount
            from starlette.staticfiles import StaticFiles

            class GalleryFiles(StaticFiles):
                """Serve the manifest from one read: it is replaced while pages poll it, and the default
                stat-then-stream response fails ("content longer than Content-Length") if that happens in between.
                On Windows, opening it during the replace itself fails briefly, so retry, then ask the page to retry."""

                def file_response(self, full_path, stat_result, scope, status_code=200):
                    if not str(full_path).endswith(".json"):
                        return super().file_response(full_path, stat_result, scope, status_code)
                    for attempt in range(5):
                        try:
                            with open(full_path, "rb") as fh:
                                body = fh.read()
                            return Response(body, status_code=status_code, media_type="application/json", headers={"Cache-Control": "no-cache"})
                        except PermissionError:
                            time.sleep(0.01 * (attempt + 1))
                    return Response(status_code=503, headers={"Retry-After": "1", "Cache-Control": "no-cache"})

            self._unmount_local()
            GALLERY_ROOT.mkdir(parents=True, exist_ok=True)
            routes = app.router.routes
            routes.insert(0, Mount(URL_GALLERY, app=GalleryFiles(directory=GALLERY_ROOT.resolve(), html=True), name=MOUNT_NAMES[0]))
            routes.insert(1, Mount(URL_PROCESSED, app=StaticFiles(directory=Path(PATH_PROCESSED).resolve()), name=MOUNT_NAMES[1]))
        except Exception as exc:
            logger.warning(f"gallery: could not serve the gallery locally: {exc}")

    @staticmethod
    def _unmount_local() -> None:
        app = getattr(sys.modules.get("photobooth.application"), "app", None)
        if app is None:
            return
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "name", None) not in MOUNT_NAMES]
