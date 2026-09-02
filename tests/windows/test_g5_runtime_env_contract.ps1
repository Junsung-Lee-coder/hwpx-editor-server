[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Write-TestUtf8 {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function New-TestManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [bool]$IncludeEnvEntry = $false,
        [bool]$CreateEnv = $false
    )
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $appPath = Join-Path $Root 'app.py'
    Write-TestUtf8 -Path $appPath -Text "print('g5')`n"
    $envPath = Join-Path $Root '.env'
    if ($CreateEnv) {
        Write-TestUtf8 -Path $envPath -Text "HWP_API_PORT=18765`r`n"
    }
    $commit = 'a' * 40
    $tree = 'b' * 40
    $files = @([ordered]@{
        path = 'app.py'
        size = [int64](Get-Item -LiteralPath $appPath).Length
        sha256 = Get-Sha256Hex -Path $appPath
    })
    if ($IncludeEnvEntry) {
        $files += [ordered]@{
            path = '.env'
            size = [int64](Get-Item -LiteralPath $envPath).Length
            sha256 = Get-Sha256Hex -Path $envPath
        }
    }
    $manifestPath = Join-Path $Root 'source-manifest.json'
    $manifest = [ordered]@{
        schema_version = 'hwpx/source-bundle/v1'
        repository = 'github:test/g5-runtime-env'
        commit = $commit
        tree = $tree
        identity_source = 'asserted-gitless'
        identity_verified = $false
        file_count = $files.Count
        files = $files
    }
    Write-TestUtf8 -Path $manifestPath -Text (($manifest | ConvertTo-Json -Depth 10) + "`n")
    return [pscustomobject]@{
        root = $Root
        manifest_path = $manifestPath
        manifest = $manifest
        manifest_sha256 = Get-Sha256Hex -Path $manifestPath
        commit = $commit
        tree = $tree
        repository = 'github:test/g5-runtime-env'
    }
}

function Get-TestManifestResult {
    param(
        [Parameter(Mandatory = $true)][object]$Fixture,
        [AllowNull()][object]$RuntimeEnvContract
    )
    $arguments = @{
        SourceRoot = [string]$Fixture.root
        ManifestPath = [string]$Fixture.manifest_path
        ExpectedRepository = [string]$Fixture.repository
        ExpectedCommit = [string]$Fixture.commit
        ExpectedTree = [string]$Fixture.tree
        ExpectedManifestSha256 = [string]$Fixture.manifest_sha256
    }
    if ($null -ne $RuntimeEnvContract) {
        $arguments.ExpectedRuntimeEnvContract = $RuntimeEnvContract
    }
    return Get-SourceManifest @arguments
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('hwpx-g5-runtime-env-' + [Guid]::NewGuid().ToString('N'))
try {
    # Load the production producer helper so this test covers marker creation,
    # not merely a hand-authored consumer object.
    $installerText = Get-Content -LiteralPath (Join-Path $root 'scripts\install_windows.ps1') -Raw -Encoding UTF8
    $producerStart = $installerText.IndexOf('function Set-InstallerRuntimeEnvProvenance')
    $producerEnd = $installerText.IndexOf('function Ensure-CandidateConfig', $producerStart)
    if ($producerStart -lt 0 -or $producerEnd -le $producerStart) {
        throw 'Installer runtime .env provenance producer helper was not found.'
    }
    Invoke-Expression $installerText.Substring($producerStart, $producerEnd - $producerStart)

    $trusted = New-TestManifest -Root (Join-Path $tempRoot 'trusted')
    $custody = Get-TestManifestResult -Fixture $trusted -RuntimeEnvContract $null
    Assert-True ([bool]$custody.ok) 'Source custody baseline did not pass before runtime generation.'

    $envPath = Join-Path $trusted.root '.env'
    Write-TestUtf8 -Path $envPath -Text "HWP_API_PORT=18765`r`n"
    $envItem = Get-Item -LiteralPath $envPath -Force
    $markerPath = Join-Path $trusted.root '.hwpx-install.json'
    $candidateGeneration = '{0}:{1}:{2}' -f $trusted.commit, $trusted.tree, $trusted.manifest_sha256
    $marker = [ordered]@{
        schema_version = 'hwpx/windows-install-marker/v1'
        repository = $trusted.repository
        commit = $trusted.commit
        tree = $trusted.tree
        source_manifest_sha256 = $trusted.manifest_sha256
        candidate_generation = $candidateGeneration
    }
    Write-JsonReceipt -Path $markerPath -Value $marker | Out-Null
    $configResult = [pscustomobject]@{
        env_path = $envPath
        env_size_after = [int64]$envItem.Length
        env_sha256_after = Get-Sha256Hex -Path $envPath
        source = 'config.example'
    }
    $provenance = Set-InstallerRuntimeEnvProvenance -InstallRoot $trusted.root -ManifestResult $custody -ConfigResult $configResult
    Assert-True ([string]$provenance.contract.provenance -ceq 'installer-generated') 'Producer did not mark config.example as installer-generated.'
    Assert-True ([string]$provenance.contract.path -ceq '.env') 'Producer did not bind the exact runtime .env relative path.'
    $accepted = Get-TestManifestResult -Fixture $trusted -RuntimeEnvContract $provenance.contract
    Assert-True ([bool]$accepted.ok) 'Expected installer-generated post-custody .env was not accepted.'
    Assert-True ([int]$accepted.mismatch_count -eq 0) 'Trusted runtime .env acceptance retained unexpected mismatches.'
    Assert-True ([bool]$accepted.runtime_env_contract_applied) 'Verifier did not report the runtime .env contract as applied.'

    $bundled = New-TestManifest -Root (Join-Path $tempRoot 'bundled') -IncludeEnvEntry $true -CreateEnv $true
    $bundledResult = Get-TestManifestResult -Fixture $bundled -RuntimeEnvContract $null
    Assert-True (-not [bool]$bundledResult.ok) 'Source-bundled .env was accepted without installer provenance.'
    Assert-True (@($bundledResult.mismatches | Where-Object { [string]$_.path -ceq '.env' }).Count -eq 1) 'Source-bundled .env rejection was not attributed to .env.'

    $untrusted = New-TestManifest -Root (Join-Path $tempRoot 'untrusted')
    $untrustedCustody = Get-TestManifestResult -Fixture $untrusted -RuntimeEnvContract $null
    Assert-True ([bool]$untrustedCustody.ok) 'Untrusted fixture source custody baseline did not pass.'
    $untrustedEnvPath = Join-Path $untrusted.root '.env'
    Write-TestUtf8 -Path $untrustedEnvPath -Text "HWP_API_PORT=29999`r`n"
    $missingContract = Get-TestManifestResult -Fixture $untrusted -RuntimeEnvContract $null
    Assert-True (-not [bool]$missingContract.ok) 'Post-custody .env without explicit provenance was accepted.'
    $wrongContract = [pscustomobject]@{
        schema_version = 'hwpx/installer-runtime-env/v1'
        provenance = 'installer-generated'
        source = 'config.example'
        path = '.env'
        install_root = [string](Get-CanonicalPath -Path $untrusted.root -RequireExisting)
        install_root_identity = Get-PathObjectIdentity -Path $untrusted.root -RequireExisting
        size = [int64](Get-Item -LiteralPath $untrustedEnvPath).Length
        sha256 = ('0' * 64)
        source_manifest_sha256 = $untrusted.manifest_sha256
        candidate_generation = '{0}:{1}:{2}' -f $untrusted.commit, $untrusted.tree, $untrusted.manifest_sha256
        created_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    $wrongHash = Get-TestManifestResult -Fixture $untrusted -RuntimeEnvContract $wrongContract
    Assert-True (-not [bool]$wrongHash.ok) 'Runtime .env with mismatched provenance bytes was accepted.'

    Write-Output 'G5_RUNTIME_ENV_CONTRACT=PASS'
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
