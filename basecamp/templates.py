"""Game templates (R-13) and importing an existing install (R-15).

A template is one JSON file describing how to run a game's dedicated server: Steam app ID,
exe, arguments, ports, ready line, stop recipe, config files and save folder. Built-in
templates ship in the repo's ``templates/`` folder; ``<data>/templates/*.json`` adds more,
and one with the same ``id`` overrides the built-in one, so a game can be added without
waiting for a release. The schema is documented in docs/08-templates.md.

Paths in a template are relative to the server's install folder unless they start with a
drive, a slash or an environment variable such as ``%LOCALAPPDATA%`` (expanded when the
form is filled, on the PC Basecamp runs on).
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from basecamp.models import LogSource, Port, ServerKind, StopStep, StopWarning

log = logging.getLogger(__name__)

TemplateSource = Literal["built-in", "data folder"]

# Fields a template can mark as not yet checked against a real install.
_CHECKABLE = {
    "steam_app_id", "exe", "args", "working_dir", "real_exe_name", "ports", "join_port_label",
    "log_source", "log_file", "ready_pattern", "ready_after_s", "stop_recipe", "config_files",
    "save_dir", "rcon_port", "min_free_ram_mb", "stop_warning",
}


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=100)
    kind: ServerKind = ServerKind.STEAM
    steam_app_id: int | None = Field(default=None, ge=1)
    exe: str = Field(min_length=1)
    args: str = ""
    working_dir: str = ""
    real_exe_name: str = ""
    ports: list[Port] = Field(min_length=1)
    join_port_label: str = ""
    log_source: LogSource = LogSource.STDOUT
    log_file: str = ""
    ready_pattern: str = ""
    ready_after_s: float = Field(default=60.0, ge=0)
    stop_recipe: list[StopStep] = Field(default_factory=list)
    stop_warning: StopWarning | None = None
    config_files: list[str] = Field(default_factory=list)
    save_dir: str = ""
    rcon_port: int | None = Field(default=None, ge=1, le=65535)
    min_free_ram_mb: int = Field(default=0, ge=0)
    notes: str = ""
    docs: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Template:
        if self.kind is ServerKind.STEAM and self.steam_app_id is None:
            raise ValueError("a steam template needs steam_app_id")
        if self.log_source is LogSource.FILE and not self.log_file.strip():
            raise ValueError("log_source 'file' needs log_file")
        labels = [p.label for p in self.ports]
        if self.join_port_label and self.join_port_label not in labels:
            raise ValueError(f"join_port_label {self.join_port_label!r} is not one of the "
                             f"port labels {labels}")
        unknown = set(self.unverified) - _CHECKABLE
        if unknown:
            raise ValueError(f"unverified lists unknown fields: {', '.join(sorted(unknown))}"
                             f" (allowed: {', '.join(sorted(_CHECKABLE))})")
        return self


@dataclass(frozen=True)
class LoadedTemplate:
    template: Template
    source: TemplateSource
    path: Path
    overrides: bool = False  # a data-folder template replacing a built-in one


@dataclass
class TemplateSet:
    templates: dict[str, LoadedTemplate] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)  # {"file", "error"}

    def get(self, template_id: str) -> LoadedTemplate:
        return self.templates[template_id]

    def by_app_id(self, app_id: int) -> LoadedTemplate | None:
        for loaded in self.templates.values():
            if loaded.template.steam_app_id == app_id:
                return loaded
        return None


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        where = ".".join(str(x) for x in err["loc"]) or "template"
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def load_file(path: Path) -> Template:
    """Read and validate one template. Raises ValueError with a readable message."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ValueError(f"can't read it: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: line {e.lineno} column {e.colno}: {e.msg}") from e
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    try:
        template = Template.model_validate(data)
    except ValidationError as e:
        raise ValueError(_describe(e)) from e
    if path.stem != template.id:
        raise ValueError(f"file name must match the id: rename it to {template.id}.json")
    return template


def load_templates(builtin_dir: Path, data_dir: Path) -> TemplateSet:
    """Built-in templates, then the data folder's (same id wins). Bad files are skipped and
    reported in ``errors`` instead of breaking the rest."""
    result = TemplateSet()
    for source, folder in (("built-in", builtin_dir), ("data folder", data_dir)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                template = load_file(path)
            except ValueError as e:
                result.errors.append({"file": f"{source}: {path.name}", "error": str(e)})
                log.warning("Template %s skipped: %s", path, e)
                continue
            existing = result.templates.get(template.id)
            if existing is not None and existing.source == source:
                result.errors.append({"file": f"{source}: {path.name}",
                                      "error": f"duplicate id {template.id!r}"})
                continue
            result.templates[template.id] = LoadedTemplate(
                template, source, path, existing is not None)  # type: ignore[arg-type]
    result.templates = dict(sorted(result.templates.items(),
                                   key=lambda kv: kv[1].template.name.lower()))
    return result


def resolve_path(install_dir: str, path: str) -> str:
    """A template path made concrete for one install (Windows rules, on any OS)."""
    if not path:
        return ""
    path = os.path.expandvars(path)
    if ntpath.isabs(path) or not install_dir:
        return ntpath.normpath(path) if ntpath.isabs(path) else path
    return ntpath.normpath(ntpath.join(install_dir, path))


def draft(template: Template, install_dir: str, name: str = "") -> dict:
    """A server form pre-filled from a template (not saved; the admin reviews it first)."""
    join = next((p.port for p in template.ports if p.label == template.join_port_label),
                template.ports[0].port)
    return {
        "name": name or template.name,
        "kind": template.kind.value,
        "template_id": template.id,
        "install_dir": install_dir,
        "exe_path": resolve_path(install_dir, template.exe),
        "args": template.args,
        "working_dir": resolve_path(install_dir, template.working_dir),
        "ports": [p.model_dump(mode="json") for p in template.ports],
        "join_port": join,
        "steam_app_id": template.steam_app_id,
        "config_files": [resolve_path(install_dir, p) for p in template.config_files],
        "save_dir": resolve_path(install_dir, template.save_dir),
        "log_source": template.log_source.value,
        "log_file": template.log_file,
        "rcon_port": template.rcon_port,
        "real_exe_name": template.real_exe_name,
        "ready_pattern": template.ready_pattern,
        "ready_after_s": template.ready_after_s,
        "min_free_ram_mb": template.min_free_ram_mb,
        "stop_recipe": [s.model_dump(mode="json") for s in template.stop_recipe],
        "stop_warning": (template.stop_warning.model_dump(mode="json")
                         if template.stop_warning else None),
        "notes": template.notes,
    }


# -- importing an existing install (R-15) -------------------------------------------------

_ACF_PAIR = re.compile(r'"([^"]*)"\s+"([^"]*)"')


@dataclass(frozen=True)
class AppManifest:
    app_id: int
    name: str
    build_id: str
    path: Path


def read_manifests(install_dir: Path) -> list[AppManifest]:
    """``steamapps/appmanifest_*.acf`` under an install folder (SteamCMD writes one per app
    it installs with ``+force_install_dir``). Only the top-level keys are read."""
    found = []
    for path in sorted((install_dir / "steamapps").glob("appmanifest_*.acf")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        values: dict[str, str] = {}
        for key, value in _ACF_PAIR.findall(text):
            values.setdefault(key.lower(), value)
        try:
            app_id = int(values.get("appid", ""))
        except ValueError:
            continue
        found.append(AppManifest(app_id, values.get("name", ""), values.get("buildid", ""),
                                 path))
    return found
