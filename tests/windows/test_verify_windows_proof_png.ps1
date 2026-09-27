[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$verifierPath = Join-Path $root 'scripts\verify_windows.ps1'
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-proof-png-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-PngFixture {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$WithContent
    )
    $bitmap = $null
    $graphics = $null
    $brush = $null
    try {
        $bitmap = New-Object -TypeName System.Drawing.Bitmap -ArgumentList @(1323, 1869)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.Clear([System.Drawing.Color]::White)
        if ($WithContent) {
            $brush = New-Object -TypeName System.Drawing.SolidBrush -ArgumentList ([System.Drawing.Color]::FromArgb(255, 32, 32, 32))
            $graphics.FillRectangle($brush, 126, 162, 1065, 1480)
            $graphics.FillRectangle($brush, 180, 300, 720, 24)
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        if ($brush) { $brush.Dispose() }
        if ($graphics) { $graphics.Dispose() }
        if ($bitmap) { $bitmap.Dispose() }
    }
}

try {
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    $validPath = Join-Path $tempRoot 'valid-page.png'
    $blankPath = Join-Path $tempRoot 'blank-page.png'
    $corruptPath = Join-Path $tempRoot 'corrupt-page.png'
    New-PngFixture -Path $validPath -WithContent
    New-PngFixture -Path $blankPath
    [System.IO.File]::WriteAllBytes($corruptPath, [byte[]](0, 1, 2, 3, 4, 5, 6, 7))

    $verifierText = [System.IO.File]::ReadAllText($verifierPath)
    $functionStart = $verifierText.IndexOf('function Test-ProofPng')
    $functionEnd = $verifierText.IndexOf('function Invoke-FixtureSequence', $functionStart)
    Assert-True ($functionStart -ge 0 -and $functionEnd -gt $functionStart) 'Test-ProofPng function boundaries were not found.'
    $proofFunction = $verifierText.Substring($functionStart, $functionEnd - $functionStart)
    $runnerSource = @'
$ErrorActionPreference = 'Stop'
$script:ProofPngAssemblyError = $null
Add-Type -AssemblyName System.Drawing -ErrorAction Stop
'@
    $runnerSource += [Environment]::NewLine
    $runnerSource += $proofFunction
    $runnerSource += [Environment]::NewLine
    $runnerSource += @'
$valid = Test-ProofPng -Path ([string]$args[0])
$blank = Test-ProofPng -Path ([string]$args[1])
$corrupt = Test-ProofPng -Path ([string]$args[2])
[pscustomobject]@{ valid = $valid; blank = $blank; corrupt = $corrupt }
'@
    $results = & ([scriptblock]::Create($runnerSource)) $validPath $blankPath $corruptPath
    $valid = $results.valid
    $blank = $results.blank
    $corrupt = $results.corrupt

    Assert-True ([bool]$valid.ok) 'A valid nonblank PNG was rejected.'
    Assert-True ([int]$valid.width -eq 1323 -and [int]$valid.height -eq 1869) 'Valid PNG dimensions were not reported.'
    Assert-True ([int]$valid.sampled_pixels -gt 0) 'The bounded PNG scan did not sample any pixels.'
    Assert-True ([int]$valid.nonempty_samples -ge 2) 'The valid PNG did not produce robust nonempty samples.'
    Assert-True (-not [bool]$blank.ok) 'A blank PNG was accepted.'
    Assert-True (-not [bool]$corrupt.ok) 'A corrupt PNG was accepted.'
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$corrupt.reason)) 'The corrupt PNG rejection had no reason.'

    Write-Output 'VERIFY_PROOF_PNG=PASS'
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
