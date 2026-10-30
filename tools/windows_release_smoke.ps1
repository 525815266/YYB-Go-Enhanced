param(
    [Parameter(Mandatory)][string]$Binary,
    [Parameter(Mandatory)][string]$Checksums,
    [Parameter(Mandatory)][string]$ExpectedVersion,
    [string]$ExpectedCommit = ''
)
$ErrorActionPreference = 'Stop'
$smokeBinary = (Resolve-Path -LiteralPath $Binary).Path
$smokeName = [IO.Path]::GetFileName($smokeBinary)
$smokeExpectedHash = @(Get-Content -LiteralPath $Checksums | Where-Object { ($_ -split '\s+')[-1] -eq $smokeName })
if ($smokeExpectedHash.Count -ne 1) { throw 'Expected exactly one binary checksum' }
$smokeHash = ($smokeExpectedHash[0] -split '\s+')[0]
if ($smokeHash -ne (Get-FileHash -LiteralPath $smokeBinary -Algorithm SHA256).Hash) { throw 'Release checksum mismatch' }
$smokeRoot = Join-Path ([IO.Path]::GetTempPath()) ('yyb-native-smoke-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$smokeResource = Join-Path $smokeRoot 'resource'
$smokeOldDriver = $env:YYB_AUTH_DRIVER
$smokeOldToken = $env:YYB_PROTOCOL_TOKEN
$smokeProcess = $null
$smokeClient = [Net.Http.HttpClient]::new()
$smokeClient.Timeout = [TimeSpan]::FromSeconds(3)
try {
    $smokeListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $smokeListener.Start()
    $smokePort = $smokeListener.LocalEndpoint.Port
    $smokeListener.Stop()
    $smokeBase = 'http://127.0.0.1:' + $smokePort
    $env:YYB_AUTH_DRIVER = 'none'
    $env:YYB_PROTOCOL_TOKEN = 'synthetic-native-smoke-token'
    $smokeProcess = Start-Process -FilePath $smokeBinary -WorkingDirectory $smokeRoot -WindowStyle Hidden -PassThru -ArgumentList @('-host', '127.0.0.1', '-port', $smokePort, '-resource-root', ('"' + $smokeResource + '"'), '-keepalive-interval', '0') -RedirectStandardOutput (Join-Path $smokeRoot 'stdout.log') -RedirectStandardError (Join-Path $smokeRoot 'stderr.log')
    $smokeReady = $false
    for ($smokeAttempt = 0; $smokeAttempt -lt 50; $smokeAttempt++) {
        if ($smokeProcess.HasExited) { throw 'Release process exited before startup' }
        try {
            if ([int]$smokeClient.GetAsync($smokeBase + '/health').GetAwaiter().GetResult().StatusCode -eq 200) { $smokeReady = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 200
    }
    if (-not $smokeReady) { throw 'Release startup timed out' }
    $smokeVersion = ($smokeClient.GetStringAsync($smokeBase + '/api/version').GetAwaiter().GetResult() | ConvertFrom-Json).data
    if ($smokeVersion.version -ne $ExpectedVersion) { throw 'Release version mismatch' }
    if ($ExpectedCommit -and $smokeVersion.commit -ne $ExpectedCommit) { throw 'Release revision mismatch' }
    $smokeHTML = $smokeClient.GetStringAsync($smokeBase + '/').GetAwaiter().GetResult()
    if (-not $smokeHTML.Contains('id="callBtn"') -or -not $smokeHTML.Contains('/static/js/platform.js')) { throw 'Console template missing' }
    foreach ($smokeTemplate in @('login', 'index', 'logs')) {
        if (-not (Test-Path -LiteralPath (Join-Path $smokeResource ('templates/' + $smokeTemplate + '.html')))) { throw ('Embedded template missing: ' + $smokeTemplate) }
    }
    $smokeScript = $smokeClient.GetStringAsync($smokeBase + '/static/js/platform.js').GetAwaiter().GetResult()
    if (-not $smokeScript.Contains('platform-shell')) { throw 'Platform asset missing' }
    if ([int]$smokeClient.GetAsync($smokeBase + '/wx/getuserinfo?ref=1').GetAwaiter().GetResult().StatusCode -ne 401) { throw 'Protocol auth gate failed' }
    [pscustomobject]@{version=$smokeVersion.version;checksum_verified=$true;actual_windows_startup=$true;embedded_templates=$true;static_assets=$true;auth_gate=$true} | ConvertTo-Json -Compress
} finally {
    $smokeClient.Dispose()
    if ($smokeProcess -and -not $smokeProcess.HasExited) { Stop-Process -Id $smokeProcess.Id }
    $env:YYB_AUTH_DRIVER = $smokeOldDriver
    $env:YYB_PROTOCOL_TOKEN = $smokeOldToken
}
