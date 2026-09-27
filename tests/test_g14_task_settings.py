from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class G14ScheduledTaskSettingsContractTests(unittest.TestCase):
    def read_script(self, name: str) -> str:
        return (SCRIPTS / name).read_text(encoding="utf-8")

    def installer_activation_block(self) -> str:
        installer = self.read_script("install_windows.ps1")
        start = installer.index("$taskActionApi = New-ScheduledTaskAction")
        end = installer.index("$receipt.task_identities_after", start)
        return installer[start:end]

    def test_installer_makes_canonical_task_settings_explicit(self) -> None:
        activation = self.installer_activation_block()
        for token in (
            "-AllowStartIfOnBatteries",
            "-DontStopIfGoingOnBatteries",
            "-StartWhenAvailable",
            "-RunOnlyIfNetworkAvailable:$false",
            "-Hidden:$false",
        ):
            with self.subTest(token=token):
                self.assertIn(token, activation)
        self.assertEqual(activation.count("New-ScheduledTaskSettingsSet"), 1)

    def test_api_and_worker_register_the_same_canonical_settings_object(self) -> None:
        activation = self.installer_activation_block()
        for token in (
            "Register-ScheduledTask -TaskName $apiTaskName",
            "Register-ScheduledTask -TaskName $workerTaskName",
            "-Settings $taskSettings",
        ):
            with self.subTest(token=token):
                self.assertIn(token, activation)
        self.assertEqual(len(re.findall(r"-Settings\s+\$taskSettings", activation)), 2)

    def test_actual_verifier_and_installer_callers_keep_the_settings_gate(self) -> None:
        installer = self.read_script("install_windows.ps1")
        verifier = self.read_script("verify_windows.ps1")
        common = self.read_script("windows_install_common.psm1")
        self.assertGreaterEqual(installer.count("Assert-TaskIdentity -Identity (Get-ScheduledTaskIdentity"), 2)
        self.assertIn("Test-CanonicalTaskSettings -Identity $identity", verifier)
        self.assertIn("function Test-CanonicalTaskSettings", common)
        for setting_name in (
            "DisallowStartIfOnBatteries",
            "StopIfGoingOnBatteries",
            "RunOnlyIfNetworkAvailable",
            "Hidden",
        ):
            with self.subTest(setting_name=setting_name):
                self.assertIn(setting_name, common)


if __name__ == "__main__":
    unittest.main()
