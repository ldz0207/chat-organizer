[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$configFile = Get-Item -LiteralPath $ConfigPath
$config = Get-Content -LiteralPath $configFile.FullName -Raw | ConvertFrom-Json
$state = $config.state_dir
$script = Join-Path $PSScriptRoot 'watcher.py'
if (Test-Path -LiteralPath (Join-Path $state 'stop-request.json')) { Write-Output 'Recency worker remains stopped by user request.'; exit 0 }
$record = Join-Path $state 'watcher.json'
if (Test-Path -LiteralPath $record) {
    $previous = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
    $process = Get-Process -Id $previous.pid -ErrorAction SilentlyContinue
    if ($process) {
        $details = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $previous.pid)
        if (-not $details.CommandLine.Contains($script)) { $process = $null }
    }
    if ($previous.status -eq 'failed' -and $previous.error -match 'denied|permission|Approved group|membership|relative order|readback|Untested') {
        throw ('Recency worker requires capability/state review: ' + $previous.error)
    }
    if ($process -and $previous.status -eq 'running') {
        if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $previous.updated_at -ge 40) { throw 'Existing worker heartbeat is stale; no duplicate writer started.' }
        Write-Output ('Recency worker online, PID=' + $previous.pid); exit 0
    }
}
foreach ($name in @('CODEX_APP_TOOLS_PIPE_PATH','CODEX_THREAD_ID','CODEX_MCP_NODE_PATH','CODEX_CLI_PATH')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw ('Missing genuine executor environment: ' + $name) }
}
if ($env:CODEX_VERSION -ne $config.backend_version) { throw 'Codex version changed; revalidate capabilities first.' }
New-Item -ItemType Directory -Path $state -Force | Out-Null
foreach ($path in @($configFile.FullName,$config.python_path,$config.app_tools_server,$script,$state)) {
    $item = Get-Item -LiteralPath $path
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Unexpected link: ' + $path) }
}
$process = Start-Process -FilePath $config.python_path -ArgumentList @('"' + $script + '"','--config','"' + $configFile.FullName + '"','--run') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $state 'stdout.log') -RedirectStandardError (Join-Path $state 'stderr.log') -PassThru
Write-Output ('Recency worker started, PID=' + $process.Id)
