[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$module = Import-Module (Join-Path $root 'scripts\windows_install_common.psm1') -Force -PassThru -DisableNameChecking
$temp = Join-Path ([IO.Path]::GetTempPath()) ('hwpx-store-alias-test-' + [Guid]::NewGuid().ToString('N'))
$family = 'PythonSoftwareFoundation.Python.3.13_' + [Guid]::NewGuid().ToString('N')
$aliasRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('Microsoft\WindowsApps\' + $family)
try {
    New-Item -ItemType Directory -Path $aliasRoot -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $temp 'image') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $temp 'candidate\.venv\Scripts') -Force | Out-Null
    $alias = Join-Path $aliasRoot 'python.exe'
    $image = Join-Path $temp 'image\python3.13.exe'
    $venv = Join-Path $temp 'candidate\.venv\Scripts\python.exe'
    foreach ($p in @($alias, $image, $venv)) { [IO.File]::WriteAllText($p, 'fixture-not-executable') }
    [IO.File]::WriteAllText((Join-Path $temp 'candidate\.venv\pyvenv.cfg'), "home = $aliasRoot`r`nversion = 3.13.14`r`nexecutable = $alias`r`n")
    & $module {
        param($temp, $family, $aliasRoot, $image, $venv)
        $script:fixturePackages = @([pscustomobject]@{
            Name = 'PythonSoftwareFoundation.Python.3.13'; PackageFamilyName = $family
            InstallLocation = (Split-Path $image); Status = 'Ok'; SignatureKind = 'Store'
        })
        function Get-AppxPackage { param($Name, $ErrorAction) return $script:fixturePackages }
        $process = [pscustomobject]@{ProcessId=40001; ExecutablePath=$image; ExecutableVersion='3.13.14'; CommandLine=('"'+$venv+'" -m app.api_server')}
        $candidate = Join-Path $temp 'candidate'
        function Test-Fixture { Test-CanonicalProcessIdentity -Process $process -RootPath $candidate -ExpectedPythonPath $venv -ModuleNames @('app.api_server') }
        if (-not (Test-Fixture)) { throw 'Registered Store alias must bind its exact package image.' }
        $package = $script:fixturePackages[0]
        $package.PackageFamilyName = 'unrelated_publisher'
        if (Test-Fixture) { throw 'Wrong registered family accepted.' }
        $package.PackageFamilyName = $family
        $package.SignatureKind = 'Developer'
        if (Test-Fixture) { throw 'Non-Store registration accepted.' }
        $package.SignatureKind = 'Store'
        $package.Status = 'Modified'
        if (Test-Fixture) { throw 'Unhealthy package registration accepted.' }
        $package.Status = 'Ok'
        $script:fixturePackages = @($package, $package)
        if (Test-Fixture) { throw 'Ambiguous registrations accepted.' }
        $script:fixturePackages = @($package)
        $process.ExecutableVersion = '3.12.1'
        if (Test-Fixture) { throw 'Wrong actual image version accepted.' }
        $process.ExecutableVersion = '3.13.14'
        $process.CommandLine = '"C:\unrelated\python.exe" -m app.api_server'
        if (Test-Fixture) { throw 'Unrelated venv command line accepted.' }
        Remove-Item Function:Get-AppxPackage
    } $temp $family $aliasRoot $image $venv
    Write-Output 'PASS: registered Store alias identity and six negative boundaries'
} finally {
    if (Test-Path -LiteralPath $aliasRoot) { Remove-Item -LiteralPath $aliasRoot -Recurse -Force }
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
