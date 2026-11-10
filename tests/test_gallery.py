import json
import sys
import time
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pluggy
import pytest
from PIL import Image

import photobooth.plugins
from photobooth_gallery import gallery as pg

PHOTOS = Path("media/processed_full")


class FakeSynchronizer:
    """Stands in for photobooth-app's Synchronizer plugin: answers share links, records uploads."""

    def __init__(self, links=("https://pics.example.com/share/#/?url={name}",)):
        self.links = list(links)
        self.added: list[tuple[list[Path], int]] = []
        self.deleted: list[list[Path]] = []
        # The real Synchronizer ignores these hooks until its own start() has run, which is normally
        # after the gallery's start() (pluggy calls the most recently registered plugin first).
        self.ready = True

    @photobooth.plugins.hookimpl
    def get_share_links(self, filepath_local, identifier):
        return [link.format(name=filepath_local.name, id=identifier) for link in self.links]

    @photobooth.plugins.hookimpl
    def collection_files_added(self, files, priority_modifier):
        if self.ready:
            self.added.append((list(files), priority_modifier))

    @photobooth.plugins.hookimpl
    def collection_files_deleted(self, files):
        if self.ready:
            self.deleted.append(list(files))

    def uploaded(self):
        return [Path(f).as_posix() for files, _ in self.added for f in files]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A photobooth-data folder with a fresh plugin manager, fake synchronizer and fake media DB."""
    monkeypatch.chdir(tmp_path)
    PHOTOS.mkdir(parents=True)

    pm = pluggy.PluginManager("photobooth-app")
    if hasattr(photobooth.plugins, "Spec"):  # stub: register the hookspecs
        pm.add_hookspecs(photobooth.plugins.Spec)
    else:  # real photobooth-app
        for spec in ("PluginManagementSpec", "PluginMediacollectionSpec"):
            pm.add_hookspecs(getattr(photobooth.plugins, spec))
    monkeypatch.setattr(pg, "pm", pm)

    sync = FakeSynchronizer()
    pm.register(sync, name="synchronizer")

    db: dict[str, pg.ItemInfo] = {}
    gallery = pg.Gallery()
    gallery._config.common.enabled = True
    gallery._config.common.event_title = "One NT <Party>"
    gallery._config.publishing.serve_locally = False
    gallery.lookup_item = lambda path: db.get(Path(path).as_posix())
    gallery.list_items = lambda: sorted(db.values(), key=lambda i: i.created_at)
    pm.register(gallery, name="photobooth_gallery.gallery")

    base = datetime(2026, 11, 9, 20, 0, 0)

    def add_photo(name, minutes=0, media_type="image", show=True, revision=0, color="red"):
        path = PHOTOS / name
        Image.new("RGB", (1200, 800), color).save(path)
        info = pg.ItemInfo(uuid.uuid4(), media_type, path, show, revision, base + timedelta(minutes=minutes))
        db[path.as_posix()] = info
        return info

    return types.SimpleNamespace(pm=pm, sync=sync, db=db, gallery=gallery, add_photo=add_photo, dir=Path("media/gallery/One-NT-Party"))


def manifest(env):
    return json.loads((env.dir / "thumbs.json").read_text(encoding="utf-8"))


def wait_for_items(env, count, timeout=10):
    """Poll the manifest the worker thread writes. On Windows a read can briefly fail while the file is replaced."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            entries = manifest(env)
            if len(entries) == count:
                return entries
        except (OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"manifest did not reach {count} entries within {timeout}s")
        time.sleep(0.05)


def run(env, kind, path=None):
    env.gallery.process(pg._Task(kind, Path(path) if path else None))


def test_slugify():
    assert pg.slugify("One NT <Party>") == "One-NT-Party"
    assert pg.slugify("Café Ünïcode 2026") == "Cafe-Unicode-2026"
    assert pg.slugify("../../etc") == "etc"
    assert pg.slugify("   ") == "event"


def test_add_creates_thumbnail_qrcode_manifest_and_page(env):
    info = env.add_photo("a.jpg")
    run(env, "init")
    run(env, "add", info.processed)

    entries = manifest(env)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["filename"] == "a.jpg"
    assert entry["full"] == "../../processed_full/a.jpg"
    assert entry["thumb"] == "thumbs/a_thumb.jpg"
    assert entry["qrcode"] == "qrcodes/a_qrcode.svg"
    assert entry["share_url"] == "https://pics.example.com/share/#/?url=a.jpg"
    assert entry["id"] == str(info.id)

    # full URL resolves to the photo from the page's folder (same layout locally and on the web host)
    assert (env.dir / entry["full"]).resolve() == info.processed.resolve()
    with Image.open(env.dir / entry["thumb"]) as thumb:
        # photobooth-app's fast JPEG scaler rounds up to 1/2, 1/4 or 1/8 of the original
        assert 400 <= max(thumb.size) < 1200
    assert (env.dir / entry["qrcode"]).read_text(encoding="utf-8").lstrip().startswith("<svg")

    page = (env.dir / "index.html").read_text(encoding="utf-8")
    assert "<title>One NT &lt;Party&gt;</title>" in page
    assert "{{" not in page

    uploaded = env.sync.uploaded()
    for name in ("index.html", "thumbs.json", "thumbs/a_thumb.jpg", "qrcodes/a_qrcode.svg"):
        assert (env.dir / name).as_posix() in uploaded
    assert all(priority == pg.SYNC_PRIORITY_MODIFIER for _, priority in env.sync.added)


def test_own_files_are_not_requeued(env):
    """Files we hand to the synchronizer come back through collection_files_added; they must be ignored."""
    env.gallery._queue = pg.queue.Queue()
    env.gallery.collection_files_added(files=[env.dir / "thumbs.json", env.dir / "thumbs" / "x.jpg"], priority_modifier=1)
    assert env.gallery._queue.empty()
    env.gallery.collection_files_added(files=[PHOTOS / "a.jpg"], priority_modifier=-1)
    assert env.gallery._queue.qsize() == 1


def test_newest_first_and_filters(env):
    old = env.add_photo("old.jpg", minutes=0)
    new = env.add_photo("new.jpg", minutes=5)
    hidden = env.add_photo("collage_part.jpg", minutes=6, show=False)
    video = env.add_photo("clip.jpg", minutes=7, media_type="video")
    for info in (new, old, hidden, video):  # arrival order should not matter
        run(env, "add", info.processed)
    run(env, "add", "media/camera_original/unknown.jpg")  # not a collection item

    assert [e["filename"] for e in manifest(env)] == ["new.jpg", "old.jpg"]
    assert not (env.dir / "thumbs" / "collage_part_thumb.jpg").exists()


def test_update_keeps_position_and_busts_cache(env):
    a = env.add_photo("a.jpg", minutes=0)
    b = env.add_photo("b.jpg", minutes=1)
    run(env, "add", a.processed)
    run(env, "add", b.processed)

    env.db[a.processed.as_posix()] = pg.ItemInfo(a.id, a.media_type, a.processed, True, 3, a.created_at)
    run(env, "update", a.processed)

    entries = manifest(env)
    assert [e["filename"] for e in entries] == ["b.jpg", "a.jpg"]
    assert entries[1]["thumb"] == "thumbs/a_thumb.jpg?v=3"
    assert entries[1]["revision"] == 3


def test_delete_removes_entry_files_and_remote_copies(env):
    a = env.add_photo("a.jpg")
    run(env, "add", a.processed)
    thumb, qr = env.dir / "thumbs/a_thumb.jpg", env.dir / "qrcodes/a_qrcode.svg"
    assert thumb.exists() and qr.exists()

    del env.db[a.processed.as_posix()]
    a.processed.unlink()
    run(env, "delete", a.processed)

    assert manifest(env) == []
    assert not thumb.exists() and not qr.exists()
    assert {p.as_posix() for p in env.sync.deleted[-1]} == {thumb.as_posix(), qr.as_posix()}
    assert (env.dir / "thumbs.json").as_posix() in env.sync.uploaded()


def test_startup_drops_entries_whose_photo_is_gone(env):
    a = env.add_photo("a.jpg")
    b = env.add_photo("b.jpg", minutes=1)
    run(env, "add", a.processed)
    run(env, "add", b.processed)

    a.processed.unlink()  # e.g. media collection cleared / file removed outside the app
    run(env, "init")
    assert [e["filename"] for e in manifest(env)] == ["b.jpg"]
    assert not (env.dir / "thumbs/a_thumb.jpg").exists()
    assert (PHOTOS / "b.jpg").exists()  # photos themselves are never deleted


def test_backfill_on_start(env):
    env.add_photo("a.jpg", minutes=0)
    env.add_photo("b.jpg", minutes=1)
    env.add_photo("hidden.jpg", minutes=2, show=False)
    env.gallery._config.media.backfill_on_start = True
    run(env, "init")
    assert [e["filename"] for e in manifest(env)] == ["b.jpg", "a.jpg"]

    run(env, "republish")  # sends the carried startup files
    env.sync.added.clear()
    run(env, "init")  # nothing changed: nothing re-uploaded (the page and manifest are only carried again)
    assert env.sync.added == []


def test_startup_files_resent_when_synchronizer_starts_later(env):
    """Found syncing to a real rclone remote: the page uploaded during startup never arrived."""
    a = env.add_photo("a.jpg")
    gone = env.add_photo("gone.jpg", minutes=1)
    run(env, "add", a.processed)
    run(env, "add", gone.processed)
    gone.processed.unlink()
    env.sync.added.clear()

    env.sync.ready = False  # restart: the Synchronizer is not running yet while the gallery initializes
    run(env, "init")
    assert env.sync.added == [] and env.sync.deleted == []

    env.sync.ready = True
    b = env.add_photo("b.jpg", minutes=2)
    run(env, "add", b.processed)
    uploaded = env.sync.uploaded()
    for name in ("index.html", "thumbs.json", "thumbs/b_thumb.jpg", "qrcodes/b_qrcode.svg"):
        assert (env.dir / name).as_posix() in uploaded
    assert {p.as_posix() for p in env.sync.deleted[0]} == {(env.dir / "thumbs/gone_thumb.jpg").as_posix(), (env.dir / "qrcodes/gone_qrcode.svg").as_posix()}

    env.sync.added.clear()
    c = env.add_photo("c.jpg", minutes=3)
    run(env, "add", c.processed)  # carried files went out once; not again
    assert (env.dir / "index.html").as_posix() not in env.sync.uploaded()


def test_startup_files_resent_by_timer_without_new_photos(env, monkeypatch):
    monkeypatch.setattr(pg, "REPUBLISH_DELAY_S", 0.3)
    env.sync.ready = False
    env.pm.hook.start()
    try:
        wait_for_items(env, 0)
        env.sync.ready = True  # the Synchronizer finished starting
        deadline = time.monotonic() + 5
        while (env.dir / "index.html").as_posix() not in env.sync.uploaded():
            assert time.monotonic() < deadline, "page was not resent"
            time.sleep(0.05)
        assert (env.dir / "thumbs.json").as_posix() in env.sync.uploaded()
    finally:
        env.pm.hook.stop()
    assert env.gallery._republish_timer is None


def test_events_are_kept_apart(env):
    a = env.add_photo("a.jpg")
    run(env, "add", a.processed)

    env.gallery._config.common.event_folder = "Wedding 2027"
    env.gallery._gallery_dir = env.gallery.gallery_dir()
    b = env.add_photo("b.jpg", minutes=1)
    run(env, "init")
    run(env, "add", b.processed)

    assert [e["filename"] for e in manifest(env)] == ["a.jpg"]
    second = json.loads(Path("media/gallery/Wedding-2027/thumbs.json").read_text(encoding="utf-8"))
    assert [e["filename"] for e in second] == ["b.jpg"]


def test_share_link_selection_and_fallback(env):
    info = env.add_photo("a.jpg")
    env.sync.links = ["https://first/{name}", "https://second/{name}"]
    env.gallery._config.qrcode.share_link_index = 1
    assert env.gallery.share_link(info) == "https://second/a.jpg"

    env.gallery._config.qrcode.share_link_index = 5
    assert env.gallery.share_link(info) == "https://first/a.jpg"

    env.sync.links = []
    assert env.gallery.share_link(info) is None
    env.gallery._config.qrcode.fallback_url = "https://host/e/media/processed_full/{filename}?id={identifier}"
    assert env.gallery.share_link(info) == f"https://host/e/media/processed_full/a.jpg?id={info.id}"


def test_no_share_link_means_no_qrcode(env):
    env.sync.links = []
    info = env.add_photo("a.jpg")
    run(env, "add", info.processed)
    entry = manifest(env)[0]
    assert "qrcode" not in entry and "share_url" not in entry


def test_corrupt_manifest_is_backed_up(env):
    env.dir.mkdir(parents=True)
    (env.dir / "thumbs.json").write_text("{not json", encoding="utf-8")
    info = env.add_photo("a.jpg")
    run(env, "add", info.processed)
    assert (env.dir / "thumbs.json.bak").read_text(encoding="utf-8") == "{not json"
    assert len(manifest(env)) == 1


def test_manifest_write_retries_while_file_is_open_on_windows(env, monkeypatch):
    """Found running the real app on Windows: replacing thumbs.json while the web server streams it fails."""
    real_replace, calls = pg.os.replace, []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(pg.os, "replace", flaky_replace)
    info = env.add_photo("a.jpg")
    run(env, "add", info.processed)
    assert [e["filename"] for e in manifest(env)] == ["a.jpg"]
    assert not list(env.dir.rglob("*.tmp"))

    monkeypatch.setattr(pg.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    monkeypatch.setattr(pg.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        pg.atomic_write(env.dir / "x.json", "{}")
    assert not list(env.dir.rglob("*.tmp"))  # temp file cleaned up after giving up


def test_custom_template(env, tmp_path):
    custom = tmp_path / "userdata" / "mine.html"
    custom.parent.mkdir()
    custom.write_text("<h1>{{TITLE}}</h1><script>fetch('{{MANIFEST}}');poll={{POLL_SECONDS}}</script>", encoding="utf-8")
    env.gallery._config.common.theme = "custom"
    env.gallery._config.common.custom_template = "userdata/mine.html"
    assert env.gallery.render_template() == "<h1>One NT &lt;Party&gt;</h1><script>fetch('thumbs.json');poll=10</script>"

    env.gallery._config.common.custom_template = "missing.html"
    assert "<title>One NT &lt;Party&gt;</title>" in env.gallery.render_template()


@pytest.mark.parametrize("theme", ["classic", "snowflake"])
def test_builtin_templates_render(env, theme):
    env.gallery._config.common.theme = theme
    text = env.gallery.render_template()
    assert "{{" not in text and "const MANIFEST = 'thumbs.json'" in text


def test_hooks_end_to_end_with_worker_thread(env):
    """Drive the plugin only through pluggy hooks, the way photobooth-app does."""
    info = env.add_photo("a.jpg")
    env.pm.hook.start()
    try:
        env.pm.hook.collection_files_added(files=[info.processed], priority_modifier=-1)
        assert [e["filename"] for e in wait_for_items(env, 1)] == ["a.jpg"]

        stats = [s for s in env.pm.hook.get_stats() if s]
        assert stats and any(sub.name == "Items in gallery" and sub.val == 1 for sub in stats[0].stats)
    finally:
        env.pm.hook.stop()
    assert env.gallery._worker is None


def test_disabled_plugin_does_nothing(env):
    env.gallery._config.common.enabled = False
    env.pm.hook.start()
    env.pm.hook.collection_files_added(files=[env.add_photo("a.jpg").processed], priority_modifier=-1)
    env.pm.hook.stop()
    assert not Path("media/gallery").exists()
    assert env.pm.hook.get_stats() == []


def test_serves_gallery_locally(env, monkeypatch):
    import importlib.util

    starlette = pytest.importorskip("starlette")
    if not (importlib.util.find_spec("httpx2") or importlib.util.find_spec("httpx")):
        pytest.skip("starlette's TestClient needs httpx2 (or httpx)")
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    # shaped like photobooth-app: a /media/{dimension}/{id} route and a catch-all SPA mount last
    app = Starlette(routes=[Route("/media/{dimension}/{id}", lambda r: PlainTextResponse("media api")),
                            Mount("/", app=lambda scope, receive, send: PlainTextResponse("spa")(scope, receive, send))])
    monkeypatch.setitem(sys.modules, "photobooth.application", types.SimpleNamespace(app=app))
    assert starlette

    info = env.add_photo("a.jpg")
    env.gallery._config.publishing.serve_locally = True
    env.gallery.start()
    try:
        env.gallery._queue.put(pg._Task("add", info.processed))
        wait_for_items(env, 1)

        client = TestClient(app)
        page = client.get("/media/gallery/One-NT-Party/")
        assert page.status_code == 200 and "One NT &lt;Party&gt;" in page.text
        manifest_response = client.get("/media/gallery/One-NT-Party/thumbs.json")
        assert manifest_response.json()[0]["filename"] == "a.jpg"
        assert manifest_response.headers["cache-control"] == "no-cache"
        assert client.get("/media/processed_full/a.jpg").headers["content-type"] == "image/jpeg"
        assert client.get("/media/gallery/One-NT-Party/qrcodes/a_qrcode.svg").status_code == 200
        assert client.get("/media/full/123").text == "media api"  # existing routes untouched

        env.gallery.start()  # container reload: mounts are not duplicated
        assert sum(getattr(r, "name", None) in pg.MOUNT_NAMES for r in app.router.routes) == 2
    finally:
        env.gallery.stop()
        env.gallery.stop()
    assert not any(getattr(r, "name", None) in pg.MOUNT_NAMES for r in app.router.routes)
    assert TestClient(app).get("/media/gallery/One-NT-Party/").text == "spa"
