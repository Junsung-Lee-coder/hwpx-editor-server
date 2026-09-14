[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g11-native-capture-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $fixtureScript = Join-Path $tempRoot 'emit_fixture_where.py'
    $malformedFixtureScript = Join-Path $tempRoot 'emit_malformed_json.py'
    $largeFixtureScript = Join-Path $tempRoot 'emit_large_output.py'
    $timeoutFixtureScript = Join-Path $tempRoot 'sleep_forever.py'
    $closedStreamsFixtureScript = Join-Path $tempRoot 'close_streams_and_sleep.py'
    $fixtureReceipt = Join-Path $tempRoot 'fixture-receipt.json'
    $failureReceipt = Join-Path $tempRoot 'failure-receipt.json'
    $boundedReceipt = Join-Path $tempRoot 'bounded-receipt.json'
    $timeoutReceipt = Join-Path $tempRoot 'timeout-receipt.json'
    $closedStreamsReceipt = Join-Path $tempRoot 'closed-streams-receipt.json'
    $faultReceipt = Join-Path $tempRoot 'fault-receipt.json'
    $fixtureSource = @'
import json
import sys

marker = chr(0xC138) + chr(0xC158) + "-" + chr(0xACBD) + chr(0xB85C)
payload = {
    "schema_version": "local-output-parser/command-bundle/v1",
    "command": "where",
    "ok": True,
    "working_copy_id": marker,
    "location": {
        "current_paragraph_preview": "E. Table " + marker + "\nline2"
    }
}
text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
sys.stdout.buffer.write(text.encode("utf-8"))
sys.stderr.buffer.write(("native-stderr-" + marker + "\n").encode("utf-8"))
sys.stdout.flush()
sys.stderr.flush()
sys.exit(7)
'@
    [System.IO.File]::WriteAllText($fixtureScript, $fixtureSource, (New-Object System.Text.UTF8Encoding($false)))

    $malformedFixtureSource = @'
import sys

sys.stdout.buffer.write(b'{"malformed":')
sys.stdout.flush()
'@
    [System.IO.File]::WriteAllText($malformedFixtureScript, $malformedFixtureSource, (New-Object System.Text.UTF8Encoding($false)))

    $largeFixtureSource = @'
import sys

sys.stdout.buffer.write(b"X" * 8192)
sys.stderr.buffer.write(b"Y" * 8192)
sys.stdout.flush()
sys.stderr.flush()
'@
    [System.IO.File]::WriteAllText($largeFixtureScript, $largeFixtureSource, (New-Object System.Text.UTF8Encoding($false)))

    $timeoutFixtureSource = @'
import time

time.sleep(30)
'@
    [System.IO.File]::WriteAllText($timeoutFixtureScript, $timeoutFixtureSource, (New-Object System.Text.UTF8Encoding($false)))

    $closedStreamsFixtureSource = @'
import os
import sys
import time

sys.stdout.flush()
sys.stderr.flush()
os.close(sys.stdout.fileno())
os.close(sys.stderr.fileno())
time.sleep(30)
'@
    [System.IO.File]::WriteAllText($closedStreamsFixtureScript, $closedStreamsFixtureSource, (New-Object System.Text.UTF8Encoding($false)))

    $result = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($fixtureScript) `
        -WorkingDirectory $tempRoot `
        -AllowNonZero `
        -ReceiptPath $fixtureReceipt

    $expectedMarker = [string]([char]0xC138) + [string]([char]0xC158) + '-' + [string]([char]0xACBD) + [string]([char]0xB85C)
    Assert-Equal 7 ([int]$result.exit_code) 'Native exit code was not preserved'
    $payload = ConvertFrom-Json -InputObject ([string]$result.stdout)
    Assert-Equal $expectedMarker ([string]$payload.working_copy_id) 'Non-ASCII working-copy identity was corrupted'
    Assert-Equal ('E. Table ' + $expectedMarker + "`nline2") ([string]$payload.location.current_paragraph_preview) 'Escaped JSON newline or non-ASCII preview was corrupted'
    Assert-True ([string]$result.stdout -match [regex]::Escape($expectedMarker)) 'UTF-8 stdout marker was not preserved'
    Assert-True ([string]$result.stderr -match [regex]::Escape($expectedMarker)) 'UTF-8 stderr marker was not preserved'
    Assert-True ([int64]$result.stdout_bytes -gt 0) 'Native stdout byte count was not captured'
    Assert-True ([int64]$result.stderr_bytes -gt 0) 'Native stderr byte count was not captured'
    $expectedStdoutBytes = [System.Text.Encoding]::UTF8.GetByteCount([string]$result.stdout)
    $expectedStderrBytes = [System.Text.Encoding]::UTF8.GetByteCount([string]$result.stderr)
    Assert-Equal ([int64]$expectedStdoutBytes) ([int64]$result.stdout_bytes) 'Native stdout byte count was not measured from the original UTF-8 byte stream'
    Assert-Equal ([int64]$expectedStderrBytes) ([int64]$result.stderr_bytes) 'Native stderr byte count was not measured from the original UTF-8 byte stream'

    $fixtureReceiptValue = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $fixtureReceipt).text)
    Assert-Equal 7 ([int]$fixtureReceiptValue.exit_code) 'Receipt did not preserve the direct native exit code'
    Assert-Equal ([int64]$result.stdout_bytes) ([int64]$fixtureReceiptValue.stdout_bytes) 'Receipt stdout byte count differs from command result'

    $malformed = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($malformedFixtureScript) `
        -WorkingDirectory $tempRoot `
        -AllowNonZero
    $malformedRejected = $false
    try {
        ConvertFrom-Json -InputObject ([string]$malformed.stdout) | Out-Null
    }
    catch {
        $malformedRejected = $true
    }
    Assert-True $malformedRejected 'Malformed native JSON was not rejected by the strict parser'

    $nativeFailureCaught = $false
    try {
        Invoke-NativeChecked `
            -FilePath $python `
            -Arguments @($fixtureScript) `
            -WorkingDirectory $tempRoot `
            -ReceiptPath $failureReceipt | Out-Null
    }
    catch {
        $nativeFailureCaught = $true
    }
    Assert-True $nativeFailureCaught 'A nonzero native command unexpectedly produced a successful call'
    $failureReceiptValue = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $failureReceipt).text)
    Assert-Equal 7 ([int]$failureReceiptValue.exit_code) 'Failure receipt lost the direct native exit code'
    Assert-True (-not [bool]$failureReceiptValue.accepted) 'Failure receipt was marked accepted'

    $bounded = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($largeFixtureScript) `
        -WorkingDirectory $tempRoot `
        -AllowNonZero `
        -MaxOutputBytes 1024 `
        -ReceiptPath $boundedReceipt
    Assert-Equal 8192 ([int64]$bounded.stdout_bytes) 'Native stdout byte count did not retain the full observed stream size'
    Assert-Equal 8192 ([int64]$bounded.stderr_bytes) 'Native stderr byte count did not retain the full observed stream size'
    Assert-Equal 1024 ([int64]$bounded.stdout_captured_bytes) 'Bounded stdout capture exceeded its byte limit'
    Assert-Equal 1024 ([int64]$bounded.stderr_captured_bytes) 'Bounded stderr capture exceeded its byte limit'
    Assert-True ([bool]$bounded.stdout_truncated) 'Bounded stdout capture did not report truncation'
    Assert-True ([bool]$bounded.stderr_truncated) 'Bounded stderr capture did not report truncation'
    $boundedReceiptValue = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $boundedReceipt).text)
    Assert-Equal 8192 ([int64]$boundedReceiptValue.stdout_bytes) 'Bounded receipt lost the full stdout byte count'
    Assert-Equal 1024 ([int64]$boundedReceiptValue.stdout_captured_bytes) 'Bounded receipt exceeded captured stdout byte limit'

    $timed = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($timeoutFixtureScript) `
        -WorkingDirectory $tempRoot `
        -AllowNonZero `
        -TimeoutSeconds 1 `
        -ReceiptPath $timeoutReceipt
    Assert-Equal -2 ([int]$timed.exit_code) 'Timed-out native process did not return the timeout exit marker'
    Assert-True ([bool]$timed.timed_out) 'Timed-out native process was not marked timed_out'
    Assert-True ([bool]$timed.process_killed) 'Timed-out native process was not killed'
    Assert-True ([int]$timed.process_id -gt 0) 'Timed-out native process id was not recorded'
    Assert-True ($null -eq (Get-Process -Id ([int]$timed.process_id) -ErrorAction SilentlyContinue)) 'Timed-out native process survived cleanup'
    $timeoutReceiptValue = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $timeoutReceipt).text)
    Assert-True ([bool]$timeoutReceiptValue.timed_out) 'Timeout receipt did not retain timeout evidence'

    $closedStreams = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($closedStreamsFixtureScript) `
        -WorkingDirectory $tempRoot `
        -AllowNonZero `
        -TimeoutSeconds 1 `
        -ReceiptPath $closedStreamsReceipt
    Assert-Equal -2 ([int]$closedStreams.exit_code) 'A process with closed output streams bypassed the timeout'
    Assert-True ([bool]$closedStreams.timed_out) 'Closed-stream timeout was not marked timed_out'
    Assert-True ([bool]$closedStreams.process_killed) 'Closed-stream timeout process was not killed'
    Assert-True ($null -eq (Get-Process -Id ([int]$closedStreams.process_id) -ErrorAction SilentlyContinue)) 'Closed-stream timeout process survived cleanup'
    $closedStreamsReceiptValue = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $closedStreamsReceipt).text)
    Assert-True ([bool]$closedStreamsReceiptValue.timed_out) 'Closed-stream timeout receipt did not retain timeout evidence'

    $hadNativeFault = Test-Path Env:HWPX_TEST_NATIVE_FAULT
    $previousNativeFault = $env:HWPX_TEST_NATIVE_FAULT
    foreach ($faultMode in @('invocation', 'capture', 'decode', 'drain', 'termination')) {
        $env:HWPX_TEST_NATIVE_FAULT = $faultMode
        $faultResult = Invoke-NativeChecked `
            -FilePath $python `
            -Arguments @($malformedFixtureScript) `
            -WorkingDirectory $tempRoot `
            -AllowNonZero `
            -ReceiptPath $faultReceipt
        Assert-True (-not [bool]$faultResult.accepted) "Injected native $faultMode fault was accepted"
        Assert-Equal $faultMode ([string]$faultResult.fault_injection) "Native fault mode was not retained for $faultMode"
        $faultEvidence = @($faultResult.invocation_error, $faultResult.capture_error, $faultResult.termination_error) -join ' '
        Assert-True (-not [string]::IsNullOrWhiteSpace($faultEvidence)) "Injected native $faultMode fault had no bounded error evidence"
    }
    if ($hadNativeFault) { $env:HWPX_TEST_NATIVE_FAULT = $previousNativeFault }
    else { Remove-Item Env:HWPX_TEST_NATIVE_FAULT -ErrorAction SilentlyContinue }

    $hadReceiptFault = Test-Path Env:HWPX_TEST_RECEIPT_FAULT
    $previousReceiptFault = $env:HWPX_TEST_RECEIPT_FAULT
    foreach ($faultMode in @('serialization', 'write', 'readback')) {
        $env:HWPX_TEST_RECEIPT_FAULT = $faultMode
        $receiptFaultCaught = $false
        try {
            Write-JsonReceipt -Path $faultReceipt -Value ([ordered]@{ schema_version = 'hwpx/test-receipt/v1'; status = 'PASS'; status_code = 0 }) | Out-Null
        }
        catch {
            $receiptFaultCaught = $true
        }
        Assert-True $receiptFaultCaught "Injected receipt $faultMode fault did not fail closed"
    }
    if ($hadReceiptFault) { $env:HWPX_TEST_RECEIPT_FAULT = $previousReceiptFault }
    else { Remove-Item Env:HWPX_TEST_RECEIPT_FAULT -ErrorAction SilentlyContinue }

    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [pscustomobject]@{
        status = 'PASS'
        exit_code = [int]$result.exit_code
        stdout_bytes = [int64]$result.stdout_bytes
        stderr_bytes = [int64]$result.stderr_bytes
        bounded_stdout_bytes = [int64]$bounded.stdout_captured_bytes
        bounded_stderr_bytes = [int64]$bounded.stderr_captured_bytes
        observed_stdout_bytes = [int64]$bounded.stdout_bytes
        observed_stderr_bytes = [int64]$bounded.stderr_bytes
        timeout_exit_code = [int]$timed.exit_code
        malformed_json_rejected = [bool]$malformedRejected
        native_failure_receipt_accepted = [bool]$failureReceiptValue.accepted
        working_copy_id = [string]$payload.working_copy_id
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
