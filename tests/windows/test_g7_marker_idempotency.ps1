[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Equal {
    param([object]$Expected, [object]$Actual, [string]$Message)
    if ($Expected -ne $Actual) {
        throw "$Message (expected='$Expected', actual='$Actual')"
    }
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Message)
    $thrown = $false
    try {
        & $Action | Out-Null
    }
    catch {
        $thrown = $true
    }
    if (-not $thrown) { throw $Message }
}

function Write-TestUtf8 {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function New-TestManifestResult {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$Tree,
        [Parameter(Mandatory = $true)][string]$ManifestSha256
    )
    return [pscustomobject]@{
        manifest = [pscustomobject]@{
            repository = $Repository
            commit = $Commit
            tree = $Tree
        }
        manifest_sha256 = $ManifestSha256
    }
}

function New-TestInstall {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [bool]$IncludeRuntimeEnv = $true
    )
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $envPath = Join-Path $Path '.env'
    Write-TestUtf8 -Path $envPath -Text "HWP_API_PORT=28777`r`n"
    $repository = 'github:test/g7-marker-idempotency'
    $commit = 'a' * 40
    $tree = 'b' * 40
    $manifestSha256 = 'c' * 64
    $candidateGeneration = '{0}:{1}:{2}' -f $commit, $tree, $manifestSha256
    $root = Get-CanonicalPath -Path $Path -RequireExisting
    $envItem = Get-Item -LiteralPath $envPath -Force
    $runtimeEnv = [ordered]@{
        schema_version = 'hwpx/installer-runtime-env/v1'
        provenance = 'installer-generated'
        source = 'config.example'
        path = '.env'
        install_root = $root
        install_root_identity = Get-PathObjectIdentity -Path $root -RequireExisting
        size = [int64]$envItem.Length
        sha256 = Get-Sha256Hex -Path $envPath
        source_manifest_sha256 = $manifestSha256
        candidate_generation = $candidateGeneration
        created_at_utc = '2026-09-03T00:00:00.0000000Z'
    }
    $marker = [ordered]@{
        schema_version = 'hwpx/windows-install-marker/v1'
        repository = $repository
        commit = $commit
        tree = $tree
        source_manifest_sha256 = $manifestSha256
        candidate_generation = $candidateGeneration
        installed_at_utc = '2026-09-03T00:00:01.0000000Z'
    }
    if ($IncludeRuntimeEnv) {
        $marker.runtime_env = [pscustomobject]$runtimeEnv
    }
    $markerPath = Join-Path $Path '.hwpx-install.json'
    Write-JsonReceipt -Path $markerPath -Value $marker | Out-Null
    return [pscustomobject]@{
        root = $root
        marker_path = $markerPath
        env_path = $envPath
        manifest = New-TestManifestResult -Repository $repository -Commit $commit -Tree $tree -ManifestSha256 $manifestSha256
        config = [pscustomobject]@{
            env_path = $envPath
            env_size_after = [int64]$envItem.Length
            env_sha256_after = Get-Sha256Hex -Path $envPath
            source = 'candidate'
        }
    }
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g7-marker-idempotency-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    $installerText = Get-Content -LiteralPath (Join-Path $root 'scripts\install_windows.ps1') -Raw -Encoding UTF8
    $producerStart = $installerText.IndexOf('function Set-InstallerRuntimeEnvProvenance')
    $producerEnd = $installerText.IndexOf('function Ensure-CandidateConfig', $producerStart)
    if ($producerStart -lt 0 -or $producerEnd -le $producerStart) {
        throw 'Installer runtime .env provenance producer helper was not found.'
    }
    Invoke-Expression $installerText.Substring($producerStart, $producerEnd - $producerStart)

    $trusted = New-TestInstall -Path (Join-Path $tempRoot 'trusted')
    $beforeBytes = [System.IO.File]::ReadAllBytes($trusted.marker_path)
    $beforeSha256 = Get-Sha256Hex -Path $trusted.marker_path

    # RED: unchanged reuse must preserve the exact marker bytes and its
    # original runtime-env provenance, even when ConfigResult reports candidate.
    $reuse = Set-InstallerRuntimeEnvProvenance `
        -InstallRoot $trusted.root `
        -ManifestResult $trusted.manifest `
        -ConfigResult $trusted.config
    $afterBytes = [System.IO.File]::ReadAllBytes($trusted.marker_path)
    Assert-True ([bool]$reuse.bytes_preserved) 'Unchanged reused install did not report marker byte preservation.'
    Assert-True (-not [bool]$reuse.marker_rewritten) 'Unchanged reused install reported an unexpected marker rewrite.'
    Assert-Equal $beforeSha256 (Get-Sha256Hex -Path $trusted.marker_path) 'Unchanged reused install changed marker SHA-256.'
    Assert-True ([System.Linq.Enumerable]::SequenceEqual($beforeBytes, $afterBytes)) 'Unchanged reused install changed marker bytes.'
    Assert-Equal 'config.example' ([string]$reuse.contract.source) 'Unchanged reuse did not preserve original runtime-env source.'
    Assert-Equal 'installer-generated' ([string]$reuse.contract.provenance) 'Unchanged reuse did not preserve original runtime-env provenance.'
    Assert-Equal '2026-09-03T00:00:00.0000000Z' ([string]$reuse.contract.created_at_utc) 'Unchanged reuse changed runtime-env creation time.'

    Start-Sleep -Milliseconds 10
    $repeat = Set-InstallerRuntimeEnvProvenance `
        -InstallRoot $trusted.root `
        -ManifestResult $trusted.manifest `
        -ConfigResult $trusted.config `
        -PreserveExistingMarkerBytes
    Assert-True ([bool]$repeat.bytes_preserved) 'Repeated unchanged reuse did not preserve marker bytes.'
    Assert-Equal $beforeSha256 (Get-Sha256Hex -Path $trusted.marker_path) 'Repeated unchanged reuse changed marker SHA-256.'

    # Candidate/source identity drift must fail closed instead of being treated
    # as reuse or silently relabeled by the marker producer.
    $driftManifest = New-TestManifestResult `
        -Repository 'github:test/g7-marker-idempotency' `
        -Commit ('d' * 40) `
        -Tree ('b' * 40) `
        -ManifestSha256 ('c' * 64)
    Assert-Throws {
        Set-InstallerRuntimeEnvProvenance `
            -InstallRoot $trusted.root `
            -ManifestResult $driftManifest `
            -ConfigResult $trusted.config `
            -PreserveExistingMarkerBytes
    } 'Candidate identity drift was accepted during reused-install marker handling.'

    # A changed runtime .env preimage must also fail closed.
    $envDrift = New-TestInstall -Path (Join-Path $tempRoot 'env-drift')
    Write-TestUtf8 -Path $envDrift.env_path -Text "HWP_API_PORT=29999`r`n"
    $envDrift.config.env_size_after = [int64](Get-Item -LiteralPath $envDrift.env_path).Length
    $envDrift.config.env_sha256_after = Get-Sha256Hex -Path $envDrift.env_path
    Assert-Throws {
        Set-InstallerRuntimeEnvProvenance `
            -InstallRoot $envDrift.root `
            -ManifestResult $envDrift.manifest `
            -ConfigResult $envDrift.config `
            -PreserveExistingMarkerBytes
    } 'Runtime .env identity drift was accepted during reused-install marker handling.'

    # A tampered marker contract must not be repaired over in a true reuse path.
    $markerDrift = New-TestInstall -Path (Join-Path $tempRoot 'marker-drift')
    $markerDriftPayload = (Read-BoundedJsonObject -Path $markerDrift.marker_path -MaxBytes 65536).value
    $markerDriftPayload.runtime_env.source_manifest_sha256 = 'e' * 64
    Write-JsonReceipt -Path $markerDrift.marker_path -Value $markerDriftPayload | Out-Null
    Assert-Throws {
        Set-InstallerRuntimeEnvProvenance `
            -InstallRoot $markerDrift.root `
            -ManifestResult $markerDrift.manifest `
            -ConfigResult $markerDrift.config `
            -PreserveExistingMarkerBytes
    } 'Runtime-env marker contract drift was overwritten instead of rejected.'

    # RED: when a marker genuinely lacks the runtime-env contract, one
    # deterministic update is required; subsequent reuse must become stable.
    $migration = New-TestInstall -Path (Join-Path $tempRoot 'migration') -IncludeRuntimeEnv:$false
    $migrationBeforeSha256 = Get-Sha256Hex -Path $migration.marker_path
    $migrationResult = Set-InstallerRuntimeEnvProvenance `
        -InstallRoot $migration.root `
        -ManifestResult $migration.manifest `
        -ConfigResult $migration.config
    $migrationAfterSha256 = Get-Sha256Hex -Path $migration.marker_path
    Assert-True (-not [bool]$migrationResult.bytes_preserved) 'Required marker migration incorrectly reported byte preservation.'
    Assert-True ([bool]$migrationResult.marker_rewritten) 'Required marker migration did not report a rewrite.'
    Assert-True ($migrationBeforeSha256 -cne $migrationAfterSha256) 'Required marker migration did not change the missing contract.'
    $migrationStableSha256 = Get-Sha256Hex -Path $migration.marker_path
    Start-Sleep -Milliseconds 10
    $migrationRepeat = Set-InstallerRuntimeEnvProvenance `
        -InstallRoot $migration.root `
        -ManifestResult $migration.manifest `
        -ConfigResult $migration.config `
        -PreserveExistingMarkerBytes
    Assert-True ([bool]$migrationRepeat.bytes_preserved) 'Migrated marker was not stable on reused-install readback.'
    Assert-Equal $migrationStableSha256 (Get-Sha256Hex -Path $migration.marker_path) 'Migrated marker changed on repeated reuse.'

    [pscustomobject]@{
        status = 'PASS'
        unchanged_marker_sha256 = $beforeSha256
        unchanged_bytes_preserved = [bool]$reuse.bytes_preserved
        identity_drift_rejected = $true
        runtime_env_drift_rejected = $true
        marker_contract_drift_rejected = $true
        migration_rewritten_once = [bool]$migrationResult.marker_rewritten
        migration_stable_marker_sha256 = $migrationStableSha256
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
