"""Configuration for the photobooth-gallery plugin.

Shown automatically in the photobooth-app admin UI (Admin > Config > Plugins) because the
plugin assigns an instance to ``self._config``. Persisted to ``config/plugin_photobooth_gallery.json``.
"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from photobooth import CONFIG_PATH
from photobooth.services.config.baseconfig import BaseConfig

MediaTypes = Literal["image", "collage", "animation", "video", "multicamera"]


class Common(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Enable the event gallery. Thumbnails, QR codes and the gallery page are generated for every new item shown in the photobooth gallery.",
    )
    event_title: str = Field(
        default="Our Event",
        description="Title shown at the top of the gallery page.",
    )
    event_folder: str = Field(
        default="",
        description="Folder name for this event's gallery, below media/gallery/. Leave empty to derive it from the title. "
        "Change it (or the title) for the next event to start a new, empty gallery; earlier galleries are kept.",
    )
    subtitle: str = Field(
        default="Thanks for using the photobooth! Tap any photo to open it.",
        description="Line of text shown under the title.",
    )
    theme: Literal["classic", "snowflake", "custom"] = Field(
        default="classic",
        description="Gallery page design. 'custom' uses the HTML file given in custom_template.",
    )
    custom_template: str = Field(
        default="",
        description="Path to your own gallery HTML template (used when theme is 'custom'), e.g. userdata/my_gallery.html. "
        "Placeholders {{TITLE}}, {{SUBTITLE}}, {{MANIFEST}} and {{POLL_SECONDS}} are replaced. pibooth-gallery templates reading thumbs.json also work.",
    )
    poll_seconds: int = Field(
        default=10,
        ge=0,
        le=600,
        description="How often an open gallery page checks for new photos (seconds). 0 disables live updates.",
    )


class Media(BaseModel):
    media_types: list[MediaTypes] = Field(
        default=["image", "collage", "animation"],
        description="Media types added to the gallery. Video and multicamera items get a short looping video as thumbnail.",
    )
    thumbnail_length: int = Field(
        default=400,
        ge=100,
        le=1500,
        description="Minimum length of the longest thumbnail side in pixels. JPEGs are scaled by photobooth-app's fast scaler "
        "(1/2, 1/4 or 1/8 of the original), so thumbnails can come out up to about twice this size.",
    )
    backfill_on_start: bool = Field(
        default=False,
        description="On startup add every item already in the photobooth collection to this event's gallery. "
        "Useful when the collection is cleared between events, or the plugin was enabled after the event started.",
    )


class Qrcode(BaseModel):
    enabled: bool = Field(
        default=True,
        description="Show a download QR code next to each full image. Uses the same share link the photobooth shows as QR code "
        "(configured in the Synchronizer plugin).",
    )
    share_link_index: int = Field(
        default=0,
        ge=0,
        description="If several share links are configured, which one to use (0 = first).",
    )
    fallback_url: str = Field(
        default="",
        description="Used when no share link is available. {filename} and {identifier} are replaced, "
        "e.g. https://example.com/event/media/processed_full/{filename}. Leave empty for no fallback.",
    )


class Publishing(BaseModel):
    sync_via_synchronizer: bool = Field(
        default=True,
        description="Hand new gallery files to the Synchronizer plugin so they are uploaded immediately together with the photos. "
        "The gallery is then online at <remote subdir>/media/gallery/<event folder>/.",
    )
    serve_locally: bool = Field(
        default=True,
        description="Serve the gallery from the photobooth itself at http://<photobooth>:8000/media/gallery/<event folder>/ "
        "(useful with a local hotspot).",
    )


class GalleryConfig(BaseConfig):
    model_config = SettingsConfigDict(
        title="Gallery",
        json_file=f"{CONFIG_PATH}plugin_photobooth_gallery.json",
        env_prefix="photobooth_gallery-",
    )

    common: Common = Common()
    media: Media = Media()
    qrcode: Qrcode = Qrcode()
    publishing: Publishing = Publishing()
