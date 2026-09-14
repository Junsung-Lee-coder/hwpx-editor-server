from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class G13RepairContractTests(unittest.TestCase):
    def read_script(self, name: str) -> str:
        return (SCRIPTS / name).read_text(encoding="utf-8")

    def test_installer_self_verification_receipt_stays_outside_candidate_root(self) -> None:
        installer = self.read_script("install_windows.ps1")
        verifier = self.read_script("verify_windows.ps1")
        self_verify_start = installer.index("$verifyScript = Join-Path $candidateRoot")
        self_verify_end = installer.index("$receipt.status =", self_verify_start)
        self_verify = installer[self_verify_start:self_verify_end]

        self.assertRegex(
            self_verify,
            r"\$verifyReceipt\s*=\s*Join-Path\s+\(\[System\.IO\.Path\]::GetTempPath\(\)\)",
        )
        self.assertNotIn("Join-Path $candidateRoot 'receipts\\verify.json'", self_verify)
        self.assertIn("'-ReceiptPath'", self_verify)
        self.assertIn("$verifyReceipt", self_verify)

        # The independent verifier must retain its fail-closed containment gate;
        # only the installer-owned caller contract is being repaired.
        self.assertIn("Test-CanonicalPathWithinRoot", verifier)
        self.assertIn("ReceiptPath must be outside the existing InstallRoot", verifier)

    def test_native_capture_has_a_codepage_safe_fallback_without_false_failure(self) -> None:
        common = self.read_script("windows_install_common.psm1")
        decode_start = common.index("function Convert-NativeBytesToText")
        decode_end = common.index("function Invoke-NativeChecked", decode_start)
        decoder = common[decode_start:decode_end]
        native_start = decode_end
        native_end = common.index("function Get-SourceManifest", native_start)
        native = common[native_start:native_end]

        self.assertIn("[System.Text.Encoding]::Default", decoder)
        self.assertRegex(decoder, r"(?i)fallback")
        self.assertIn("decode_fallback", decoder)
        self.assertIn("stdout_encoding", native)
        self.assertIn("stderr_encoding", native)
        self.assertIn("capture_mode = 'raw-byte-pipes'", native)
        verifier = self.read_script("verify_windows.ps1")
        for field in (
            "stdout_encoding",
            "stderr_encoding",
            "stdout_decode_fallback",
            "stderr_decode_fallback",
            "stdout_decode_error",
            "stderr_decode_error",
        ):
            with self.subTest(verifier_field=field):
                self.assertIn(field, verifier)

        # All documented native callers must consume the helper's acceptance
        # result instead of treating a zero exit code as sufficient.
        for name in ("install_windows.ps1", "verify_windows.ps1", "writer_v1.ps1"):
            caller = self.read_script(name)
            with self.subTest(name=name):
                self.assertIn("Invoke-NativeChecked", caller)
                self.assertRegex(caller, r"(?s)accepted.*(?:exit_code|exit code)")

    def test_native_fallback_stays_bounded_at_byte_and_text_boundaries(self) -> None:
        common = self.read_script("windows_install_common.psm1")
        self.assertIn("$script:MaxNativeOutputBytes", common)
        self.assertIn("MaxOutputBytes", common)
        self.assertIn("stdout_captured_bytes", common)
        self.assertIn("stderr_captured_bytes", common)
        self.assertIn("stdout_truncated", common)
        self.assertIn("stderr_truncated", common)
        self.assertRegex(common, r"(?s)Convert-NativeBytesToText\s+-Bytes\s+\$stdoutRaw.*?Convert-NativeBytesToText\s+-Bytes\s+\$stderrRaw")


if __name__ == "__main__":
    unittest.main()
