from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class G19ExternalIdentityBindingTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (SCRIPTS / name).read_text(encoding="utf-8")

    def test_ps51_identity_comparison_parenthesizes_command_invocation(self) -> None:
        common = self.read("windows_install_common.psm1")
        self.assertRegex(
            common,
            r"\(ConvertTo-RepositoryIdentity\s+-Value\s+\(\[string\]\$manifest\.repository\)\)\s+-cne",
        )
        self.assertNotIn(
            "ConvertTo-RepositoryIdentity -Value ([string]$manifest.repository) -cne",
            common,
        )

    def test_installer_exposes_and_forwards_external_identity_binding(self) -> None:
        installer = self.read("install_windows.ps1")
        parameter_block = installer[: installer.index("\n)\n") + 3]
        for token in (
            "[string]$ExpectedRepository",
            "[string]$ExpectedCommit",
            "[string]$ExpectedTree",
            "[string]$ExpectedManifestSha256",
        ):
            with self.subTest(token=token):
                self.assertIn(token, parameter_block)

        manifest_lookup = installer[
            installer.index("$manifestPath = Find-InstallerManifest") : installer.index(
                "$receipt.checks.source_manifest", installer.index("$manifestPath = Find-InstallerManifest")
            )
        ]
        for token in (
            "-ExpectedRepository $ExpectedRepository",
            "-ExpectedCommit $ExpectedCommit",
            "-ExpectedTree $ExpectedTree",
            "-ExpectedManifestSha256 $ExpectedManifestSha256",
        ):
            with self.subTest(token=token):
                self.assertIn(token, manifest_lookup)
        self.assertNotIn("([string]$manifestResult.manifest.repository)", manifest_lookup)

    def test_gitless_source_requires_complete_external_binding_and_reports_it_verified(self) -> None:
        common = self.read("windows_install_common.psm1")
        manifest_function = common[
            common.index("function Get-SourceManifest") : common.index(
                "function Resolve-WindowsPrincipalIdentity"
            )
        ]
        self.assertRegex(
            manifest_function,
            r"(?s)identitySource\s+-eq\s+'asserted-gitless'.*?externalIdentityCount\s+-ne\s+3",
        )
        self.assertIn("identityBindingVerified", manifest_function)
        self.assertRegex(
            manifest_function,
            r"(?s)verified\s*=\s*\$identityBindingVerified",
        )

    def test_external_binding_rejects_malformed_commit_tree_and_manifest_hash(self) -> None:
        common = self.read("windows_install_common.psm1")
        manifest_function = common[
            common.index("function Get-SourceManifest") : common.index(
                "function Resolve-WindowsPrincipalIdentity"
            )
        ]
        self.assertIn("ExpectedCommit -notmatch", manifest_function)
        self.assertIn("ExpectedTree -notmatch", manifest_function)
        self.assertIn("ExpectedManifestSha256 -notmatch", manifest_function)
        self.assertRegex(
            manifest_function,
            r"(?:\[string\]\$)?ExpectedCommit\s+-notmatch\s+'\^\[0-9a-fA-F\]\{40\}\(\[0-9a-fA-F\]\{24\}\)\?\$'",
        )
        self.assertRegex(
            manifest_function,
            r"(?:\[string\]\$)?ExpectedTree\s+-notmatch\s+'\^\[0-9a-fA-F\]\{40\}\(\[0-9a-fA-F\]\{24\}\)\?\$'",
        )

    def test_independent_git_identity_rejects_dirty_tracked_checkout(self) -> None:
        common = self.read("windows_install_common.psm1")
        identity_function = common[
            common.index("function Get-IndependentGitIdentity") : common.index(
                "function Get-GitSourceMemberIdentity"
            )
        ]
        self.assertIn("status", identity_function)
        self.assertIn("--porcelain=v1", identity_function)
        self.assertIn("dirty", identity_function.lower())

    def test_verifier_requires_the_same_independent_binding_for_gitless_receivers(self) -> None:
        verifier = self.read("verify_windows.ps1")
        for token in (
            "[string]$ExpectedRepository",
            "[string]$ExpectedCommit",
            "[string]$ExpectedTree",
            "[string]$ExpectedManifestSha256",
            "-ExpectedRepository $ExpectedRepository",
            "-ExpectedCommit $ExpectedCommit",
            "-ExpectedTree $ExpectedTree",
            "-ExpectedManifestSha256 $ExpectedManifestSha256",
        ):
            with self.subTest(token=token):
                self.assertIn(token, verifier)
        self.assertIn("identity_binding.verified", verifier)


if __name__ == "__main__":
    unittest.main()
