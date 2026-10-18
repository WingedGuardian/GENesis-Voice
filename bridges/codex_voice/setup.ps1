$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$options = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__OPTIONS__')) | ConvertFrom-Json
$package = $options.package
if (!$package) {
    $package = Join-Path (Split-Path (Get-Command codex.cmd -ErrorAction Stop).Source) 'node_modules\@openai\codex'
}
$package = (Resolve-Path -LiteralPath $package).Path
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
    if ([IO.File]::ReadAllText($program) -cne $source) { throw 'Existing Windows asset has different contents' }
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
$description = & node.exe $program --describe | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Windows helper validation failed' }
$description | Add-Member -NotePropertyName asset -NotePropertyValue $digest
$description | ConvertTo-Json -Compress
