param([string]$SetupSource, [string]$HostSource)
# Isolated Windows integration test. Uses an installed native helper as a
# read-only fixture; fake npm copies its native resources into test-owned
# directories. No audio devices, package downloads or live installations.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (!$SetupSource) { $SetupSource = Get-Content -LiteralPath "$PSScriptRoot\..\setup.ps1" -Raw }
if (!$HostSource) { $HostSource = Get-Content -LiteralPath "$PSScriptRoot\..\host.cjs" -Raw }
$arch = (& node.exe -p process.arch).Trim()
$base = Join-Path (Split-Path (Get-Command codex.cmd).Source) 'node_modules\@openai\codex'
$vendor = Join-Path $base ('node_modules\@openai\codex-win32-' + $arch + '\vendor')
$native = @(Get-ChildItem -LiteralPath $vendor -Directory | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName 'codex-resources\voice\manifest.json')
})
if ($native.Count -ne 1) { throw 'Install a native Windows Codex package for the read-only fixture' }
$manifest = Get-Content -LiteralPath (Join-Path $native[0].FullName 'codex-resources\voice\manifest.json') -Raw | ConvertFrom-Json
$originalHash = (Get-FileHash -LiteralPath (Join-Path $native[0].FullName 'codex-resources\voice\bin\codex-voice-host.exe')).Hash
$sandbox = Join-Path $env:TEMP ('genesis-voice-proof-' + [Guid]::NewGuid().ToString('N'))
$savedLocal = $env:LOCALAPPDATA
$savedPackage = $env:GENESIS_TEST_NATIVE
$savedFault = $env:GENESIS_TEST_FAULT
$savedLog = $env:GENESIS_TEST_NPM_LOG
New-Item -ItemType Directory -Path $sandbox | Out-Null
$env:GENESIS_TEST_NATIVE = $native[0].FullName
$env:GENESIS_TEST_NPM_LOG = Join-Path $sandbox 'npm.log'
$fakeNpm = @'
function npm.cmd {
    if ($env:GENESIS_TEST_FAULT -eq 'network') { throw 'simulated registry unavailable' }
    $prefix = $args[([Array]::IndexOf($args, '--prefix') + 1)]
    $target = Join-Path $prefix 'node_modules\@openai\codex\vendor\fixture\codex-resources'
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $env:GENESIS_TEST_NATIVE 'codex-resources\voice') -Destination $target -Recurse
    $bin = Join-Path (Split-Path $target) 'bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $env:GENESIS_TEST_NATIVE 'bin\codex.exe') -Destination $bin
    Add-Content -LiteralPath $env:GENESIS_TEST_NPM_LOG -Value 'install'
    $metadata = Join-Path $target 'voice\manifest.json'
    if ($env:GENESIS_TEST_FAULT -eq 'json') { Set-Content -LiteralPath $metadata -Value '{' }
    if ($env:GENESIS_TEST_FAULT -eq 'missing') {
        Remove-Item -LiteralPath (Join-Path $target 'voice\bin\codex-voice-host.exe')
    }
    if ($env:GENESIS_TEST_FAULT -eq 'hash') {
        $data = Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json
        $data.sha256.'codex-resources/voice/bin/codex-voice-host.exe' = '0' * 64
        $data | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $metadata
    }
    $global:LASTEXITCODE = 0
}
'@
function Invoke-Provision {
    $options = @{spec=('@openai/codex@' + $manifest.appVersion + '-win32-' + $arch);
                 version=$manifest.appVersion; build=$manifest.buildCommit}
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($options | ConvertTo-Json -Compress)))
    $source = $SetupSource.Replace('__OPTIONS__', $encoded).Replace('__SOURCE__',
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($HostSource)))
    $loader = '& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))'
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Process -Id $PID).Path
    $info.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' +
        [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($loader))
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $process.StandardInput.Write($fakeNpm + "`n" + $source)
    $process.StandardInput.Close()
    $output = $process.StandardOutput.ReadToEnd()
    $errorText = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $result = @{code=$process.ExitCode; data=($output | ConvertFrom-Json); error=$errorText}
    $process.Dispose()
    return $result
}
try {
    $env:GENESIS_TEST_FAULT = ''
    $env:LOCALAPPDATA = Join-Path $sandbox 'success'
    $first = Invoke-Provision
    if ($first.code -ne 0 -or $first.data.buildCommit -cne $manifest.buildCommit) {
        throw ('Fresh staging failed: ' + ($first | ConvertTo-Json -Depth 10 -Compress))
    }
    Write-Output 'Verified fresh Windows staging'
    $second = Invoke-Provision
    if ($second.code -ne 0 -or $second.data.asset -cne $first.data.asset -or
        @(Get-Content -LiteralPath $env:GENESIS_TEST_NPM_LOG).Count -ne 1) { throw 'Idempotent reuse failed' }
    foreach ($fault in @('json', 'missing', 'hash', 'network')) {
        Write-Output ('Checking Windows failure: ' + $fault)
        $env:GENESIS_TEST_FAULT = $fault
        $env:LOCALAPPDATA = Join-Path $sandbox $fault
        $result = Invoke-Provision
        if ($result.code -eq 0 -or $result.data.held -ne ($fault -ne 'network')) { throw "Wrong failure classification: $fault" }
        if (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA ('Genesis\CodexVoice\builds\' + $manifest.buildCommit))) {
            throw "An invalid build was published: $fault"
        }
    }
    if ((Get-FileHash -LiteralPath (Join-Path $native[0].FullName 'codex-resources\voice\bin\codex-voice-host.exe')).Hash -cne $originalHash) {
        throw 'Global helper fixture was changed'
    }
    Write-Output 'PASS: fresh Windows staging, immutable reuse, malformed JSON, missing file, hash failure, transient registry failure; global helper unchanged; no audio'
} finally {
    $env:LOCALAPPDATA = $savedLocal
    $env:GENESIS_TEST_NATIVE = $savedPackage
    $env:GENESIS_TEST_FAULT = $savedFault
    $env:GENESIS_TEST_NPM_LOG = $savedLog
    Remove-Item -LiteralPath $sandbox -Recurse -Force
}
