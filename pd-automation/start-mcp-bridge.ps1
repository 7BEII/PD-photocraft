param(
    [int]$Port = 7878,
    [string]$Root = "",
    [string]$BinDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TokenFile = Join-Path $RepoRoot ".pd-runtime\control.token"

if (-not (Test-Path $TokenFile)) {
    Write-Error "Control token not found at $TokenFile. Run .\pd-automation\launch-gui.ps1 first."
}

if (-not $Root) { $Root = $RepoRoot }

$CliExe = $null
$Candidates = @()
if ($BinDir) { $Candidates += $BinDir }
if ($env:PHOTOCRAFT_BIN_DIR) { $Candidates += $env:PHOTOCRAFT_BIN_DIR }
$Candidates += (Join-Path $RepoRoot "target\release")
Get-ChildItem -Path $RepoRoot -Directory -Filter "photocraft-*-portable*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    ForEach-Object { $Candidates += $_.FullName }

foreach ($dir in $Candidates) {
    $p = Join-Path $dir "photocraft-cli.exe"
    if (Test-Path $p) { $CliExe = $p; break }
}

if (-not $CliExe) {
    Write-Error "Could not find photocraft-cli.exe. Pass -BinDir or place a portable build in the repo root."
}

& $CliExe mcp `
    --bridge "127.0.0.1:$Port" `
    --control-token-file $TokenFile `
    --automation-read-root $Root `
    --automation-write-root $Root
