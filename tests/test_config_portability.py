from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class ConfigPortabilityTests(unittest.TestCase):
    def test_pdftoppm_legacy_environment_name_is_loaded(self) -> None:
        with patch.dict(os.environ, {"HWP_PDFTOPPM": r"C:\tools\pdftoppm.exe"}, clear=False):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.pdftoppm_path, r"C:\tools\pdftoppm.exe")

    def test_pdftoppm_path_alias_is_supported(self) -> None:
        with patch.dict(os.environ, {"HWP_PDFTOPPM_PATH": r"C:\tools\alternate.exe"}, clear=False):
            os.environ.pop("HWP_PDFTOPPM", None)
            settings = Settings(_env_file=None)
        self.assertEqual(settings.pdftoppm_path, r"C:\tools\alternate.exe")

    def test_api_port_defaults_to_8765_without_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.api_port, 8765)

    def test_api_port_override_is_loaded_from_environment(self) -> None:
        with patch.dict(os.environ, {"HWP_API_PORT": "18765"}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.api_port, 18765)

    def test_api_port_rejects_out_of_range_values(self) -> None:
        with patch.dict(os.environ, {"HWP_API_PORT": "65536"}, clear=True):
            with self.assertRaises(ValueError):
                Settings(_env_file=None)


if __name__ == "__main__":
    unittest.main()
