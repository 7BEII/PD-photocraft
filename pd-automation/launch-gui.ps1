param(
    [int]$Port = 7878,
    [string]$Root = "",
    [string]$Open = "",
    [string]$BinDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyScript = Join-Path $ScriptDir "pd_photocraft.py"

$ArgsList = @($PyScript, "launch-gui", "--port", "$Port")
if ($Root) { $ArgsList += @("--root", $Root) }
if ($Open) { $ArgsList += @("--open", $Open) }
if ($BinDir) { $ArgsList += @("--bin-dir", $BinDir) }

python @ArgsList
