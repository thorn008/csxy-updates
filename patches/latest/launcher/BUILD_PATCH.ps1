param([string]$Version='', [string]$Notes='程序修复与功能更新')
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python=Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
if(-not (Test-Path -LiteralPath $python)){$python=Join-Path $root 'runtime\python.exe'}
if(-not (Test-Path -LiteralPath $python)){throw '找不到 Python 运行环境。'}
$arguments=@('-X','utf8',(Join-Path $PSScriptRoot 'build_patch.py'),'--notes',$Notes)
if($Version){$arguments+=@('--version',$Version)}
& $python @arguments
if($LASTEXITCODE -ne 0){throw '补丁生成失败。'}
