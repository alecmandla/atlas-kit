"""Tests for atlas_config. Stdlib unittest; uses only temp dirs, never the real vault.

Run:  cd _shared && python3 -m unittest -q test_atlas_config
  or: python3 -m pytest -q _shared/test_atlas_config.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_config as ac  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        # Isolate from any real ~/.config/atlas/config.json on the machine.
        self._p = mock.patch.object(ac, "USER_CONFIG_PATH", self.dir / "nope" / "config.json")
        self._p.start()
        self.addCleanup(self._p.stop)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(ac.ENV_VAR, None)
        ac.reset()
        self.addCleanup(ac.reset)

    def write(self, data, name="config.json") -> Path:
        p = self.dir / name
        p.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        return p


class DefaultsTests(_Base):
    def test_defaults_match_reference_layout(self):
        cfg = ac.load()
        self.assertIsNone(cfg.source)
        self.assertEqual(cfg.vault_root, Path.home() / "Vault")
        self.assertEqual(cfg.folder("inbox"), cfg.vault_root / "00 - Inbox")
        self.assertEqual(cfg.folder("daily"), cfg.vault_root / "10 - Daily Notes")
        self.assertEqual(cfg.folder("projects"), cfg.vault_root / "20 - Projects")
        self.assertEqual(cfg.folder("areas"), cfg.vault_root / "30 - Areas")
        self.assertEqual(cfg.folder("resources"), cfg.vault_root / "40 - Resources")
        self.assertEqual(cfg.folder("archive"), cfg.vault_root / "50 - Archive")
        self.assertEqual(cfg.folder("meta"), cfg.vault_root / "60 - Meta")
        self.assertEqual(cfg.folder("attachments"), cfg.vault_root / "99 - Attachments")
        self.assertEqual(cfg.folder("crm"), cfg.vault_root / "CRM")
        self.assertEqual(cfg.folder("clippings"), cfg.vault_root / "Clippings")
        self.assertEqual(cfg.folder("raw"), cfg.vault_root / "raw")
        self.assertEqual(cfg.folder("wiki"), cfg.vault_root / "wiki")
        self.assertEqual(cfg.owner_name, "")
        self.assertEqual(cfg.timezone, "America/Los_Angeles")

    def test_folder_name_and_rel(self):
        cfg = ac.load()
        self.assertEqual(cfg.folder_name("meta"), "60 - Meta")
        self.assertEqual(cfg.rel("meta", "Dashboards", "Wiki-Lint.md"), "60 - Meta/Dashboards/Wiki-Lint.md")
        with self.assertRaises(KeyError):
            cfg.folder("bogus")

    def test_load_is_cached(self):
        self.assertIs(ac.load(), ac.load())

    def test_vault_from_arg(self):
        self.assertEqual(ac.vault_from_arg(None), ac.load().vault_root)
        self.assertEqual(ac.vault_from_arg(""), ac.load().vault_root)
        self.assertEqual(ac.vault_from_arg("/x/y"), Path("/x/y"))
        self.assertEqual(ac.vault_from_arg(Path("/x/y")), Path("/x/y"))
        self.assertEqual(ac.vault_from_arg("~/v"), Path.home() / "v")


class FileTests(_Base):
    def test_env_var_wins_and_overrides_everything(self):
        p = self.write({
            "vault_root": str(self.dir / "MyVault"),
            "owner_name": "Pat",
            "timezone": "Europe/Berlin",
            "folders": {"projects": "Projects", "wiki": "knowledge"},
        })
        os.environ[ac.ENV_VAR] = str(p)
        cfg = ac.load()
        self.assertEqual(cfg.source, p)
        self.assertEqual(cfg.vault_root, self.dir / "MyVault")
        self.assertEqual(cfg.owner_name, "Pat")
        self.assertEqual(cfg.timezone, "Europe/Berlin")
        self.assertEqual(cfg.folder("projects"), self.dir / "MyVault" / "Projects")
        self.assertEqual(cfg.folder("wiki"), self.dir / "MyVault" / "knowledge")
        # untouched keys keep defaults
        self.assertEqual(cfg.folder_name("areas"), "30 - Areas")

    def test_user_config_path_is_second(self):
        user = self.dir / "cfg" / "config.json"
        user.parent.mkdir()
        user.write_text(json.dumps({"vault_root": "~/Other"}), encoding="utf-8")
        with mock.patch.object(ac, "USER_CONFIG_PATH", user):
            ac.reset()
            cfg = ac.load()
        self.assertEqual(cfg.source, user)
        self.assertEqual(cfg.vault_root, Path.home() / "Other")

    def test_tilde_expanded(self):
        p = self.write({"vault_root": "~/SomeVault"})
        os.environ[ac.ENV_VAR] = str(p)
        self.assertEqual(ac.load().vault_root, Path.home() / "SomeVault")

    def test_unknown_and_doc_keys_ignored(self):
        p = self.write({"_doc": "hi", "nonsense": 1,
                        "folders": {"_doc": "x", "unknown": "y", "raw": "sources"}})
        os.environ[ac.ENV_VAR] = str(p)
        self.assertEqual(ac.load().folder_name("raw"), "sources")

    def test_example_file_loads_to_defaults(self):
        example = Path(__file__).resolve().parents[1] / "atlas.config.example.json"
        os.environ[ac.ENV_VAR] = str(example)
        cfg = ac.load()
        self.assertEqual(cfg.vault_root, Path.home() / "Vault")
        self.assertEqual(dict(cfg.folders), dict(ac.DEFAULT_FOLDERS))
        self.assertEqual(cfg.timezone, ac.DEFAULT_TIMEZONE)
        self.assertEqual(cfg.owner_name, ac.DEFAULT_OWNER_NAME)

    def test_malformed_json_names_path(self):
        p = self.write("{not json")
        os.environ[ac.ENV_VAR] = str(p)
        with self.assertRaises(ac.ConfigError) as cm:
            ac.load()
        self.assertIn(str(p), str(cm.exception))

    def test_wrong_types_name_path(self):
        for bad in ({"vault_root": 5}, {"folders": []}, {"folders": {"raw": 1}}, [1, 2],
                    {"vault_root": ""}, {"folders": {"raw": "a/b"}}):
            p = self.write(bad)
            os.environ[ac.ENV_VAR] = str(p)
            ac.reset()
            with self.assertRaises(ac.ConfigError) as cm:
                ac.load()
            self.assertIn(str(p), str(cm.exception))

    def test_env_var_missing_file_errors(self):
        os.environ[ac.ENV_VAR] = str(self.dir / "missing.json")
        with self.assertRaises(ac.ConfigError) as cm:
            ac.load()
        self.assertIn("missing.json", str(cm.exception))

    def test_blank_env_var_means_unset(self):
        os.environ[ac.ENV_VAR] = "   "
        self.assertIsNone(ac.load().source)


if __name__ == "__main__":
    unittest.main()
