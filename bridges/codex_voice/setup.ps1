$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$options = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__OPTIONS__')) | ConvertFrom-Json
function Refuse-Build($message) {
    $failure = [System.InvalidOperationException]::new($message)
    $failure.Data['held'] = $true
    throw $failure
}
try {
function Assert-VoiceBuild($root, $version, $build) {
    try {
    $manifest = Get-Content -LiteralPath (Join-Path $root 'codex-resources\voice\manifest.json') -Raw | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $manifest.buildCommit -cne $build -or
        $manifest.appVersion -cne $version) { Refuse-Build 'Windows package build mismatch' }
    if (!$manifest.sha256 -or @($manifest.sha256.PSObject.Properties).Count -eq 0) { Refuse-Build 'Empty voice hash manifest' }
    foreach ($hash in $manifest.sha256.PSObject.Properties) {
        $relative = $hash.Name.Replace('/', '\')
        $file = [IO.Path]::GetFullPath((Join-Path $root $relative))
        $voiceRoot = (Join-Path $root 'codex-resources\voice') + '\'
        $nativeExe = Join-Path $root 'bin\codex.exe'
        if ((!$file.StartsWith($voiceRoot, [StringComparison]::OrdinalIgnoreCase) -and
             ![string]::Equals($file, $nativeExe, [StringComparison]::OrdinalIgnoreCase)) -or
            (Get-Item -LiteralPath $file).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $hash.Value) {
            Refuse-Build ('Windows voice resource hash mismatch: ' + $hash.Name)
        }
    }
    $nativeBuild = & (Join-Path $root 'codex-resources\voice\bin\codex-voice-host.exe') --build-commit
    if ($LASTEXITCODE -ne 0 -or ($nativeBuild -join '').Trim() -cne $build) {
        Refuse-Build 'Native Windows helper build mismatch'
    }
    } catch {
        Refuse-Build ('Windows build validation failed: ' + $_.Exception.Message)
    }
}
$package = $options.package
if ($options.spec) {
    if ($options.build -notmatch '^[a-f0-9]{40}$' -or
        $options.version -notmatch '^\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?$' -or
        $options.spec -notmatch ('^@openai/codex@' + [Regex]::Escape($options.version) + '-win32-(x64|arm64)$')) {
        Refuse-Build 'Invalid exact-build package request'
    }
    $builds = Join-Path $env:LOCALAPPDATA 'Genesis\CodexVoice\builds'
    New-Item -ItemType Directory -Path $builds -Force | Out-Null
    $package = Join-Path $builds $options.build
    if (!(Test-Path -LiteralPath $package)) {
        $stage = Join-Path $builds ('.prepare-' + [Guid]::NewGuid().ToString('N'))
        $publish = Join-Path $stage 'publish'
        try {
            New-Item -ItemType Directory -Path $stage -Force | Out-Null
            & npm.cmd install --prefix (Join-Path $stage 'npm') --ignore-scripts --no-audit --no-fund --save-exact $options.spec | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Matching Windows package installation failed' }
            $vendor = Join-Path $stage 'npm\node_modules\@openai\codex\vendor'
            $roots = @(Get-ChildItem -LiteralPath $vendor -Directory | Where-Object {
                Test-Path -LiteralPath (Join-Path $_.FullName 'codex-resources\voice\manifest.json')
            })
            if ($roots.Count -ne 1) { Refuse-Build 'Ambiguous native Windows package' }
            Move-Item -LiteralPath $roots[0].FullName -Destination $publish
            Assert-VoiceBuild $publish $options.version $options.build
            # Only complete build directories become visible. Another Linux client
            # may have published the same build while this one was downloading.
            try { [IO.Directory]::Move($publish, $package) }
            catch { if (!(Test-Path -LiteralPath $package)) { throw } }
        } finally {
            if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
        }
    }
}
if (!$package) {
    $package = Join-Path (Split-Path (Get-Command codex.cmd -ErrorAction Stop).Source) 'node_modules\@openai\codex'
}
$package = (Resolve-Path -LiteralPath $package).Path
if ($options.spec) {
    Assert-VoiceBuild $package $options.version $options.build
}
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__SOURCE__'))
$source = $source.Replace('__PACKAGE_ROOT__', ($package | ConvertTo-Json -Compress))
$bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($source)
$digest = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
$directory = Join-Path $env:LOCALAPPDATA ('Genesis\CodexVoice\' + $digest)
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$program = Join-Path $directory 'host.cjs'
$temporary = Join-Path $directory ([Guid]::NewGuid().ToString('N') + '.tmp')
try {
    [IO.File]::WriteAllBytes($temporary, $bytes)
    try { [IO.File]::Move($temporary, $program) } catch { if (!(Test-Path -LiteralPath $program)) { throw } }
    if ([IO.File]::ReadAllText($program) -cne $source) { Refuse-Build 'Existing Windows asset has different contents' }
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
$description = & node.exe $program --describe | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { Refuse-Build 'Windows helper validation failed' }
$description | Add-Member -NotePropertyName asset -NotePropertyValue $digest
$description | ConvertTo-Json -Compress
} catch {
    if ($options.spec) {
        @{ held = ($_.Exception.Data['held'] -eq $true); error = $_.Exception.Message } | ConvertTo-Json -Compress
        exit 1
    }
    throw
}
