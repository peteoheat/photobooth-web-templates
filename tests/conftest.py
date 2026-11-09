"""Test setup.

If photobooth-app is installed the real modules are used. Otherwise a minimal stand-in for the
handful of photobooth modules the plugin imports is registered, mirroring photobooth-app v9:
same pluggy project name and hookspecs, same path constants, same config base class.
"""

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_photobooth_stub():
    import pluggy
    from PIL import Image
    from pydantic_settings import BaseSettings, SettingsConfigDict

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, mod)
        return mod

    module("photobooth", MEDIA_PATH="./media/", PATH_PROCESSED="./media/processed_full/", CONFIG_PATH="./config/", __path__=[])
    module("photobooth.models", __path__=[])

    @dataclass
    class SubStats:
        name: str
        val: object
        decimals: int = 0
        unit: str = ""

    @dataclass
    class GenericStats:
        id: str
        name: str
        actions: list = field(default_factory=list)
        stats: list = field(default_factory=list)

    module("photobooth.models.genericstats", SubStats=SubStats, GenericStats=GenericStats)

    hookspec = pluggy.HookspecMarker("photobooth-app")
    hookimpl = pluggy.HookimplMarker("photobooth-app")

    class Spec:
        @hookspec
        def start(self): ...

        @hookspec
        def stop(self): ...

        @hookspec
        def get_stats(self): ...

        @hookspec
        def collection_files_added(self, files, priority_modifier): ...

        @hookspec
        def collection_files_updated(self, files): ...

        @hookspec
        def collection_files_deleted(self, files): ...

        @hookspec
        def get_share_links(self, filepath_local, identifier): ...

    class BasePlugin:
        def __class_getitem__(cls, _):
            return cls

    module("photobooth.plugins", hookimpl=hookimpl, hookspec=hookspec, Spec=Spec, pm=None, __path__=[])
    module("photobooth.plugins.base_plugin", BasePlugin=BasePlugin)
    module("photobooth.services", __path__=[])
    module("photobooth.services.config", __path__=[])

    class BaseConfig(BaseSettings):
        model_config = SettingsConfigDict(extra="ignore")

    module("photobooth.services.config.baseconfig", BaseConfig=BaseConfig)

    def resize(filepath_in, filepath_out, scaled_min_length):
        with Image.open(filepath_in) as im:
            im.thumbnail((scaled_min_length, scaled_min_length))
            im.save(filepath_out)

    module("photobooth.utils", __path__=[])
    module("photobooth.utils.media_resizer", resize=resize)


try:
    import photobooth  # noqa: F401
except ImportError:
    _install_photobooth_stub()
    STUBBED = True
else:
    STUBBED = False
