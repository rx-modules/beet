__all__ = [
    "Project",
    "Context",
    "Plugin",
    "PluginSpec",
    "PluginError",
    "PluginImportError",
]


import re
import sys
import json
from datetime import datetime
from contextlib import contextmanager
from collections import deque, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import (
    ClassVar,
    Protocol,
    NamedTuple,
    Union,
    Sequence,
    Iterator,
    Set,
    Deque,
    Tuple,
    DefaultDict,
)

from .pack import Pack, File
from .assets import ResourcePack
from .data import DataPack
from .cache import MultiCache
from .utils import FileSystemPath, extra_field, import_from_string


class Plugin(Protocol):
    def __call__(self, ctx: "Context"):
        pass


PluginSpec = Union[Plugin, str]


class PluginError(Exception):
    pass


class PluginImportError(PluginError):
    pass


class Context(NamedTuple):
    directory: Path
    output_directory: Path
    meta: dict
    cache: MultiCache
    assets: ResourcePack
    data: DataPack
    pipeline: Deque[PluginSpec]
    applied_plugins: Set[Plugin]
    beet_default: str
    current_time: datetime
    counters: DefaultDict[str, int]

    @property
    def packs(self) -> Tuple[Pack, Pack]:
        return (self.assets, self.data)

    def apply(self, plugin: PluginSpec, force: bool = False):
        try:
            func: Plugin = (
                import_from_string(plugin, default_member=self.beet_default)
                if isinstance(plugin, str)
                else plugin
            )
        except PluginError:
            raise
        except Exception as exc:
            raise PluginImportError(plugin) from exc

        if func in self.applied_plugins and not force:
            return

        self.applied_plugins.add(func)

        try:
            func(self)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(func) from exc.with_traceback(
                getattr(exc.__traceback__, "tb_next", exc.__traceback__)
            )

    @contextmanager
    def override(self, **kwargs):
        backup = {key: self.meta[key] for key in kwargs & self.meta.keys()}
        self.meta.update(**kwargs)
        try:
            yield
        finally:
            self.meta.update(backup)

    def generate(self, item: File) -> str:
        pack = next(
            pack
            for pack in self.packs
            if type(item) in pack.namespace_type.container_fields
        )
        template = self.meta.get("generate_template", "beet:generated/{type}_{id:08X}")
        name = self.generate_name(
            template.replace("{type}", type(item).__name__.lower())
        )
        pack[name] = item
        return name

    def generate_name(self, template: str) -> str:
        self.counters[template] += 1
        return template.format(id=self.counters[template])


@dataclass
class Project:
    name: str
    description: str
    author: str
    version: str

    directory: FileSystemPath
    pipeline: Sequence[PluginSpec]
    meta: dict

    output_directory: str = extra_field(default="generated")

    resource_pack_name: str = extra_field(default="{normalized_name}_resources")
    resource_pack_format: int = extra_field(default=ResourcePack.latest_pack_format)
    resource_pack_zipped: bool = extra_field(default=False)
    resource_pack_description: str = extra_field(
        default="{description}\n\nVersion {version}\nBy {author}",
    )

    data_pack_name: str = extra_field(default="{normalized_name}")
    data_pack_format: int = extra_field(default=DataPack.latest_pack_format)
    data_pack_zipped: bool = extra_field(default=False)
    data_pack_description: str = extra_field(
        default="{description}\n\nVersion {version}\nBy {author}",
    )

    cache_directory: ClassVar[str] = ".beet_cache"
    beet_default: ClassVar[str] = "beet_default"

    @classmethod
    def from_config(cls, config_file: FileSystemPath) -> "Project":
        config_path = Path(config_file).resolve()

        config = json.loads(config_path.read_text())
        meta = config.get("meta", {})
        pipeline = config.get("pipeline", [])

        if config.get("prelude", True):
            pipeline.insert(0, "beet.prelude")

        return cls(
            name=config.get("name", "Untitled"),
            description=config.get("description", "Generated by beet"),
            author=config.get("author", "Unknown"),
            version=config.get("version", "0.0.0"),
            directory=config_path.parent,
            pipeline=pipeline,
            meta=meta,
            **{
                key: value
                for key in [
                    "output_directory",
                    "resource_pack_name",
                    "resource_pack_format",
                    "resource_pack_zipped",
                    "resource_pack_description",
                    "data_pack_name",
                    "data_pack_format",
                    "data_pack_zipped",
                    "data_pack_description",
                ]
                if (value := meta.get(key))
            },
        )

    @contextmanager
    def context(self) -> Iterator[Context]:
        project_path = Path(self.directory).resolve()
        path_entry = str(project_path)

        output_directory = project_path / self.output_directory
        output_directory.mkdir(parents=True, exist_ok=True)

        variables = {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "version": self.version,
            "normalized_name": re.sub(r"[^a-z0-9]+", "_", self.name.lower()),
        }

        sys.path.append(path_entry)

        try:
            with MultiCache(project_path / self.cache_directory) as cache:
                yield Context(
                    directory=project_path,
                    output_directory=output_directory,
                    meta=deepcopy(self.meta),
                    cache=cache,
                    assets=ResourcePack(
                        self.resource_pack_name.format_map(variables),
                        self.resource_pack_description.format_map(variables),
                        self.resource_pack_format,
                        self.resource_pack_zipped,
                    ),
                    data=DataPack(
                        self.data_pack_name.format_map(variables),
                        self.data_pack_description.format_map(variables),
                        self.data_pack_format,
                        self.data_pack_zipped,
                    ),
                    pipeline=deque(self.pipeline),
                    applied_plugins=set(),
                    beet_default=self.beet_default,
                    current_time=datetime.now(),
                    counters=defaultdict(int),
                )
        finally:
            sys.path.remove(path_entry)

            imported_modules = [
                name
                for name, module in sys.modules.items()
                if (filename := getattr(module, "__file__", None))
                and filename.startswith(path_entry)
            ]

            for name in imported_modules:
                del sys.modules[name]

    def build(self) -> Context:
        with self.context() as ctx:
            while ctx.pipeline:
                ctx.apply(ctx.pipeline.popleft())
            return ctx
