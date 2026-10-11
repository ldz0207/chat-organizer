[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Path $config.state_dir -Force | Out-Null
@{requested_at=(Get-Date).ToString('o');reason='user or maintenance stop'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $config.state_dir 'stop-request.json') -Encoding utf8
Write-Output 'Stop requested; let the current native call finish. Resuming needs explicit authorization.'
