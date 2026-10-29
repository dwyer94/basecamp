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
MAX_BYTES = 256 * 1024  # a template added from the page (D-129); the built-ins are ~5 KB
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Fields a template can mark as not yet checked against a real install.
_CHECKABLE = {
    "steam_app_id", "exe", "args", "working_dir", "real_exe_name", "ports", "join_port_label",
    "log_source", "log_file", "ready_pattern", "ready_after_s", "stop_recipe", "config_files",
    "save_dir", "rcon_port", "rest_port", "min_free_ram_mb", "stop_warning", "steam_installdir",
}
# A folder name SteamCMD might use under steamapps\common: no path separators, no drive,
# none of Windows' reserved characters, no space or dot at either end ("." and "..").
_FOLDER_NAME = (r'^(|[^\\/:*?"<>|\x00-\x1f .]'
                r'([^\\/:*?"<>|\x00-\x1f]{0,98}[^\\/:*?"<>|\x00-\x1f .])?)$')


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=100)
    kind: ServerKind = ServerKind.STEAM
    steam_app_id: int | None = Field(default=None, ge=1)
    # The folder SteamCMD installs the app into under steamapps\common (the app's
    # "installdir" on Steam). Default install folder for new servers; "" = the id.
    steam_installdir: str = Field(default="", pattern=_FOLDER_NAME)
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
    rest_port: int | None = Field(default=None, ge=1, le=65535)
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


def parse(text: str) -> Template:
    """Validate a template's JSON text, as strictly as a file (D-31). Raises ValueError with
    a readable message."""
    try:
        data = json.loads(text.removeprefix("\ufeff"))
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: line {e.lineno} column {e.colno}: {e.msg}") from e
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    try:
        return Template.model_validate(data)
    except ValidationError as e:
        raise ValueError(_describe(e)) from e


def load_file(path: Path) -> Template:
    """Read and validate one template. Raises ValueError with a readable message."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ValueError(f"can't read it: {e}") from e
    template = parse(text)
    if path.stem != template.id:
        raise ValueError(f"file name must match the id: rename it to {template.id}.json")
    return template


def save_data_template(data_dir: Path, template_id: str, text: str) -> Path:
    """Write a checked template's text as ``<data_dir>\\<id>.json`` (via a temp file, so a
    half-written file is never read)."""
    if not ID_PATTERN.fullmatch(template_id):
        raise ValueError(f"bad template id {template_id!r}")
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{template_id}.json"
    tmp = data_dir / f".{template_id}.json.tmp"  # not *.json, so never loaded
    tmp.write_text(text.removeprefix("\ufeff"), encoding="utf-8", newline="")
    os.replace(tmp, path)
    return path


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
        "rest_port": template.rest_port,
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
    installdir: str = ""  # the folder under steamapps\common, for a library install


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
                                 path, values.get("installdir", "")))
    return found


def library_root(install_dir: Path) -> Path | None:
    """For ``<library>\\steamapps\\common\\<folder>``: ``<library>``, else None."""
    parent = install_dir.parent
    if parent.name.lower() == "common" and parent.parent.name.lower() == "steamapps":
        return parent.parent.parent
    return None


def manifests_for(install_dir: Path) -> list[AppManifest]:
    """The install's own ``steamapps\\appmanifest_*.acf`` (SteamCMD with
    ``+force_install_dir``). Failing that, for a folder in a library's ``steamapps\\common``
    (SteamCMD without it, or the Steam client), the library's manifests whose ``installdir``
    is this folder."""
    own = read_manifests(install_dir)
    if own:
        return own
    library = library_root(install_dir)
    if library is None:
        return []
    name = install_dir.name.lower()
    return [m for m in read_manifests(library) if m.installdir.lower() == name]


# -- the default install folder and SteamCMD's library (D-125) -------------------------

_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def folder_name(template: Template | None, server_name: str = "") -> str:
    """The folder a new server goes in under ``steamapps\\common``: the template's
    ``steam_installdir``, else its id, else the server name made safe for a folder."""
    if template is not None:
        return template.steam_installdir or template.id
    return _BAD_NAME_CHARS.sub(" ", server_name).strip(" .")[:100]


def common_dir(steamcmd_exe: Path) -> Path:
    """``steamapps\\common`` next to steamcmd.exe, where SteamCMD installs apps by default."""
    return steamcmd_exe.parent / "steamapps" / "common"


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.strip().strip('"'))) if path.strip() else ""


def _is_link(path: Path) -> bool:
    """A symlink or a junction (or any other reparse point): never listed or followed."""
    try:
        return path.is_symlink() or path.is_junction() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & 0x400)  # REPARSE_POINT
    except OSError:
        return True


def list_library(steamcmd_exe: Path, servers: list[tuple[str, str]]) -> dict:
    """The folders in SteamCMD's ``steamapps\\common``, for the Add form's pick-list.

    ``servers`` is ``(name, install_dir)`` of each Basecamp server, to say which folders are
    taken. Only that one folder's direct children are listed; links are skipped. Each says
    which app's ``appmanifest_<appid>.acf`` in ``steamapps`` names it (``installdir``)."""
    common = common_dir(steamcmd_exe)
    out: dict = {"steamcmd": str(steamcmd_exe), "steamcmd_found": steamcmd_exe.is_file(),
                 "common": str(common), "exists": False, "folders": [], "error": ""}
    if not out["steamcmd_found"]:
        out["error"] = (f"SteamCMD isn't at {steamcmd_exe}. Set its path on the Settings "
                        "page to get a default install folder and this list.")
        return out
    if not common.is_dir() or _is_link(common):
        return out
    out["exists"] = True
    by_dir = {m.installdir.lower(): m for m in read_manifests(common.parent.parent)
              if m.installdir}
    used = {_norm(d): name for name, d in servers if d.strip()}
    try:
        children = sorted(common.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        out["error"] = f"Can't read {common}: {e}"
        return out
    for child in children:
        if _is_link(child) or not child.is_dir():
            continue
        own = read_manifests(child)  # installed with +force_install_dir
        manifest = own[0] if own else by_dir.get(child.name.lower())
        out["folders"].append({
            "name": child.name, "path": str(child),
            "app_id": manifest.app_id if manifest else None,
            "app_name": manifest.name if manifest else "",
            "used_by": used.get(_norm(str(child)), ""),
        })
    return out
