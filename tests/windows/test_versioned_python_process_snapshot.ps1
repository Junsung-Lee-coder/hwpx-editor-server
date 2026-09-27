[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$m=Import-Module (Join-Path $repo 'scripts\windows_install_common.psm1') -Force -PassThru -DisableNameChecking
$root=Join-Path ([IO.Path]::GetTempPath()) ('hwpx-versioned-process-'+[Guid]::NewGuid().ToString('N'))
try {
 New-Item -ItemType Directory -Path (Join-Path $root '.venv\Scripts') -Force | Out-Null
 [IO.File]::WriteAllText((Join-Path $root '.venv\Scripts\python.exe'),'fixture')
 $tokens=$null;$parseErrors=$null
 $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'scripts\verify_windows.ps1'),[ref]$tokens,[ref]$parseErrors)
 $worker=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-VerifierWorker'},$true).Extent.Text
 & $m {
  param($root,$worker)
  $install=$root
  function Get-CimInstance {
   param($ClassName,$Filter,$ErrorAction)
   foreach($id in @(41001,41002)){
    [pscustomobject]@{ProcessId=$id;ParentProcessId=41000;Name='python3.13.exe';ExecutablePath='C:\fixture-package\python3.13.exe';CommandLine=('"'+(Join-Path $root '.venv\Scripts\python.exe')+'" -m app.worker');CreationDate='fixture-time'}
   }
  }
  function Test-CanonicalProcessIdentity {param($Process,$RootPath,$ExpectedPythonPath,$ExpectedArguments,$ModuleNames) return $Process.ProcessId -eq 41001}
  function Get-ProcessGenerationIdentity {param($ProcessId) return 'win-filetime:fixture'}
  Invoke-Expression $worker
  $snapshot=@(Get-InstallProcessSnapshot -RootPath $root)
  $verification=Test-VerifierWorker
  if($snapshot.Count -ne 1 -or $snapshot[0].process_id -ne 41001){throw 'Snapshot omitted authenticated versioned Python image or included unowned image'}
  if(!$verification.ok -or $verification.process_count -ne 1 -or $verification.processes[0].process_id -ne 41001){throw 'Verifier omitted authenticated versioned Python image or included unowned image'}
 } $root $worker
 Write-Output 'PASS: snapshot and verifier enumerate only authenticated versioned Python process'
}finally{if(Test-Path $root){Remove-Item -LiteralPath $root -Recurse -Force}}
