"""Atlas config layer: one JSON file tells every engine script where the vault
lives and what its folders are called.

Stdlib only (DEC-021: headless/launchd runs die on third-party imports).

Resolution order (first hit wins):
  1. $ATLAS_CONFIG              explicit path to a JSON file
  2. ~/.config/atlas/config.json
  3. built-in defaults          the reference layout shown below

Schema (every key optional; unknown keys and keys starting with "_" are ignored):

    {
      "vault_root": "~/Vault",
      "owner_name": "",
      "timezone": "America/Los_Angeles",
      "folders": {
        "inbox":       "00 - Inbox",
        "daily":       "10 - Daily Notes",
        "projects":    "20 - Projects",
        "areas":       "30 - Areas",
        "resources":   "40 - Resources",
        "archive":     "50 - Archive",
        "meta":        "60 - Meta",
        "attachments": "99 - Attachments",
        "crm":         "CRM",
        "clippings":   "Clippings",
        "raw":         "raw",
        "wiki":        "wiki"
      }
    }

Usage from a skill script (two levels below the repo root):

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
    import atlas_config

    CFG = atlas_config.load()
    RAW_GMAIL = CFG.folder("raw") / "gmail"        # absolute Path under vault_root
    PARA = [CFG.folder_name("projects"), ...]      # bare folder name for note text

A malformed config file raises ConfigError naming the offending path; a
missing default file silently falls back to the built-in defaults.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_FOLDERS",
    "DEFAULT_OWNER_NAME",
    "DEFAULT_TIMEZONE",
    "DEFAULT_VAULT_ROOT",
    "ENV_VAR",
    "FOLDER_KEYS",
    "USER_CONFIG_PATH",
    "load",
    "reset",
    "vault_from_arg",
]

ENV_VAR = "ATLAS_CONFIG"
USER_CONFIG_PATH = Path.home() / ".config" / "atlas" / "config.json"

DEFAULT_VAULT_ROOT = "~/Vault"
DEFAULT_OWNER_NAME = ""
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_FOLDERS: Mapping[str, str] = {
    "inbox": "00 - Inbox",
    "daily": "10 - Daily Notes",
    "projects": "20 - Projects",
    "areas": "30 - Areas",
    "resources": "40 - Resources",
    "archive": "50 - Archive",
    "meta": "60 - Meta",
    "attachments": "99 - Attachments",
    "crm": "CRM",
    "clippings": "Clippings",
    "raw": "raw",
    "wiki": "wiki",
}
FOLDER_KEYS = tuple(DEFAULT_FOLDERS)


class ConfigError(ValueError):
    """Raised when a config file exists but cannot be used. The message names the path."""


@dataclass(frozen=True)
class Config:
    vault_root: Path
    owner_name: str
    timezone: str
    folders: Mapping[str, str]
    source: Optional[Path]  # the file the values came from; None means built-in defaults

    def folder_name(self, key: str) -> str:
        """The bare folder name for `key` (e.g. "20 - Projects"), for use inside
        note text, Dataview queries, and vault-relative strings."""
        try:
            return self.folders[key]
        except KeyError:
            raise KeyError(f"unknown Atlas folder key {key!r}; known keys: {', '.join(FOLDER_KEYS)}") from None

    def folder(self, key: str) -> Path:
        """Absolute path of the named folder under vault_root."""
        return self.vault_root / self.folder_name(key)

    def rel(self, key: str, *parts: str) -> str:
        """Vault-relative POSIX string, e.g. rel("meta", "Dashboards", "X.md") ->
        "60 - Meta/Dashboards/X.md". For wikilinks and frontmatter values."""
        return "/".join((self.folder_name(key),) + tuple(parts))


# ---------------------------------------------------------------- loading ---

def _config_path() -> Optional[Path]:
    """Which file to read, or None for built-in defaults."""
    explicit = os.environ.get(ENV_VAR, "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"{ENV_VAR} points to {p}, which does not exist or is not a file")
        return p
    if USER_CONFIG_PATH.is_file():
        return USER_CONFIG_PATH
    return None


def _expect(value: Any, typ: type, what: str, source: Path) -> Any:
    if not isinstance(value, typ):
        raise ConfigError(
            f"Atlas config {source}: {what} must be a {typ.__name__}, got {type(value).__name__}"
        )
    return value


def from_mapping(data: Mapping[str, Any], source: Optional[Path] = None) -> Config:
    """Build a Config from an already-parsed mapping. Missing keys take defaults;
    unknown keys and keys beginning with "_" are ignored."""
    src = source or Path("<defaults>")
    _expect(data, dict, "top level", src)

    vault_raw = data.get("vault_root", DEFAULT_VAULT_ROOT)
    _expect(vault_raw, str, '"vault_root"', src)
    if not vault_raw.strip():
        raise ConfigError(f'Atlas config {src}: "vault_root" must not be empty')
    vault_root = Path(os.path.expanduser(vault_raw))

    owner_name = _expect(data.get("owner_name", DEFAULT_OWNER_NAME), str, '"owner_name"', src)
    timezone = _expect(data.get("timezone", DEFAULT_TIMEZONE), str, '"timezone"', src)

    folders = dict(DEFAULT_FOLDERS)
    user_folders = data.get("folders", {})
    _expect(user_folders, dict, '"folders"', src)
    for key, name in user_folders.items():
        if key.startswith("_") or key not in DEFAULT_FOLDERS:
            continue
        _expect(name, str, f'"folders.{key}"', src)
        if not name.strip() or "/" in name or "\\" in name or name in (".", ".."):
            raise ConfigError(
                f'Atlas config {src}: "folders.{key}" must be a single folder name, got {name!r}'
            )
        folders[key] = name

    return Config(
        vault_root=vault_root,
        owner_name=owner_name,
        timezone=timezone,
        folders=folders,
        source=source,
    )


def _read(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Atlas config {path} could not be read: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Atlas config {path} is not valid JSON: {e}") from e
    return from_mapping(data, source=path)


_cache: Optional[Config] = None


def load() -> Config:
    """Return the process-wide Config, reading the file at most once."""
    global _cache
    if _cache is None:
        path = _config_path()
        _cache = _read(path) if path else from_mapping({}, source=None)
    return _cache


def reset() -> None:
    """Drop the cached Config so the next load() re-resolves. For tests."""
    global _cache
    _cache = None


def vault_from_arg(value: Any) -> Path:
    """Resolve a `--vault` CLI value: an explicit value wins, otherwise the
    configured vault_root. Keeps `--help` free of personal paths."""
    if value:
        return Path(os.path.expanduser(str(value)))
    return load().vault_root
