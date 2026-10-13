param([switch]$CheckOnly, [string]$PreviewPath, [string]$GMPreviewPath, [string]$UpdatePreviewPath, [string]$PortPreviewPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ConfigPath = Join-Path $PSScriptRoot 'launcher.json'
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'Theme.ps1')
. (Join-Path $PSScriptRoot 'GMTools.ps1')
. (Join-Path $PSScriptRoot 'HelpWindow.ps1')
. (Join-Path $PSScriptRoot 'UpdateWindow.ps1')
function Get-GamePortOwners {
  $ports=@(8000,20560,28000)
  $endpoints=@()
  foreach($row in @(Get-NetTCPConnection -ErrorAction Stop | Where-Object {$_.LocalPort -in $ports -and $_.State -ne 'TimeWait'})){
    $endpoints+=[pscustomobject]@{ProcessId=[int]$row.OwningProcess;Port=[int]$row.LocalPort;Protocol='TCP'}
  }
  foreach($row in @(Get-NetUDPEndpoint -ErrorAction Stop | Where-Object {$_.LocalPort -in 20560,28000})){
    $endpoints+=[pscustomobject]@{ProcessId=[int]$row.OwningProcess;Port=[int]$row.LocalPort;Protocol='UDP'}
  }
  foreach($group in @($endpoints|Group-Object ProcessId)){
    $ownerId=[int]$group.Name;$name='系统保留';$ticks=0
    try{$process=Get-Process -Id $ownerId -ErrorAction Stop;$name=$process.ProcessName;$ticks=$process.StartTime.ToUniversalTime().Ticks}catch{}
    [pscustomobject]@{ProcessId=$ownerId;Name=$name;CreationTicks=$ticks;Endpoints=@($group.Group|ForEach-Object {'{0} {1}' -f $_.Protocol,$_.Port}|Sort-Object -Unique)}
  }
}

function Clear-GamePortOwners($Owners,$QueryFunction){
  if(@(Get-Process -Name csxy -ErrorAction SilentlyContinue).Count){throw '请先退出所有游戏客户端，再清理端口。'}
  $killed=New-Object 'Collections.Generic.List[int]';$failures=New-Object 'Collections.Generic.List[string]'
  foreach($owner in @($Owners)){
    $ownerId=[int]$owner.ProcessId
    if($ownerId -le 4 -or $ownerId -eq $PID){$failures.Add("PID $ownerId 为系统或启动器进程，无法结束。");continue}
    # Recheck ownership and creation time: a recycled PID must not be killed.
    $live=@(& $QueryFunction|Where-Object {$_.ProcessId -eq $ownerId})
    if(-not $live.Count){continue}
    try{
      $process=Get-Process -Id $ownerId -ErrorAction Stop
      if(-not $owner.CreationTicks -or $process.StartTime.ToUniversalTime().Ticks -ne [long]$owner.CreationTicks){
        throw '进程身份已变化或无法读取，请刷新；权限不足时以管理员重开启动器。'
      }
      Stop-Process -InputObject $process -Force -ErrorAction Stop
      $null=$process.WaitForExit(2000)
      $killed.Add($ownerId)
    }catch{$failures.Add("$($owner.Name) (PID $ownerId)：$($_.Exception.Message)")}
  }
  return [pscustomobject]@{Killed=$killed.ToArray();Failures=$failures.ToArray();Remaining=@(& $QueryFunction)}
}

function Show-PortTools([string]$PreviewPath=''){
  if($script:Busy -and -not $PreviewPath){Set-Status '服务端正在启动，请等待启动结束后再清理端口。';return}
  $window=New-Object CSXY.UI.Window;$window.Text='创世西游 · 端口清理';$window.StartPosition='CenterParent'
  $window.ClientSize=New-Object Drawing.Size(820,565);$window.MaximizeBox=$false
  $window.AutoScaleMode='Dpi';$window.BackColor=$script:Theme.Background;$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
  $null=New-Text $window '端口清理' 30 22 700 44 24 $true
  $null=New-Text $window 'TCP 8000 / 20560 / 28000  ·  UDP 20560 / 28000' 32 77 740 25 10 $false $script:Theme.Muted
  $card=New-Card $window 30 118 760 261
  $document=New-Object CSXY.UI.DocumentView;$document.Location=New-Object Drawing.Point(22,18);$document.Size=New-Object Drawing.Size(716,225)
  $document.BackColor=$script:Theme.Card;$document.ForeColor=$script:Theme.Ink;$document.Font=$window.Font;$card.Controls.Add($document)
  $null=New-Text $window '点击结束下列占用进程，再返回首页启动服务。请先退出游戏。' 32 399 740 26 10 $false $script:Theme.Muted
  $message=New-Text $window '' 32 443 740 41 9
  $refresh=New-Action $window '刷新占用' 30 502 190 40 {}
  $elevate=New-Action $window '以管理员重开' 237 502 230 40 {}
  $clear=New-Action $window '结束占用进程' 485 502 305 40 {} $true
  $context=@{owners=@();attempted=$false;restart=$false};$preview=[bool]$PreviewPath
  $queryFunction=${function:Get-GamePortOwners};$clearFunction=${function:Clear-GamePortOwners}
  $reload={
    try{
      $context.owners=@(if($preview){[pscustomobject]@{Name='python';ProcessId=12345;CreationTicks=1;Endpoints=@('TCP 8000','TCP 20560','TCP 28000','UDP 20560','UDP 28000')}}else{& $queryFunction})
      if($context.owners.Count){
        $document.Text=(@($context.owners|ForEach-Object {"$($_.Name)    PID $($_.ProcessId)`r`n$($_.Endpoints -join '  /  ')"}) -join "`r`n`r`n")
      }else{$document.Text='启动所需端口没有活动的占用进程，可以返回首页启动服务。'}
      $clear.Enabled=$context.owners.Count -gt 0
    }catch{$clear.Enabled=$false;$message.Text='无法读取端口占用：'+$_.Exception.Message}
  }.GetNewClosure()
  $refresh.Add_Click({$message.Text='';& $reload}.GetNewClosure())
  $clear.Add_Click({
    if($preview){return}
    $clear.Enabled=$false;$refresh.Enabled=$false;$elevate.Enabled=$false
    try{
      $result=& $clearFunction $context.owners $queryFunction;$context.attempted=$true
      & $reload
      $message.Text=if($result.Failures.Count){$result.Failures -join '；'}elseif($result.Remaining.Count){'仍有进程占用端口，请刷新后重试。'}else{'端口已释放。返回首页点击「一键开玩」即可。'}
    }catch{$message.Text=$_.Exception.Message}
    finally{$refresh.Enabled=$true;$elevate.Enabled=$true;if($context.owners.Count){$clear.Enabled=$true}}
  }.GetNewClosure())
  $launcherPath=Join-Path $Root 'launcher/CSXYLauncher.ps1'
  $elevate.Add_Click({
    if($preview){return}
    try{
      $null=Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList ('-NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "'+$launcherPath+'"')
      $context.restart=$true;$window.Close()
    }catch{$message.Text='管理员启动未完成：'+$_.Exception.Message}
  }.GetNewClosure())
  & $reload
  Add-WindowChrome $window '创世西游 / 端口清理 · V1.1' $false
  try{if($PreviewPath){Save-FormPreview $window $PreviewPath}else{[void]$window.ShowDialog($form)}}finally{$window.Dispose()}
  if($context.attempted){$script:Busy=$false;$script:GameAfterReady=$false;Set-Status '端口清理已完成，请查看占用状态后重新启动服务。'}
  if($context.restart){$form.Close()}
}

$script:ServerProcess = $null
$script:Busy = $false
$script:GameAfterReady = $false
$script:StartupDeadline = $null
$script:ServerOut = $null
$script:StatusLabel = $null

function Resolve-Relative([string]$Path) {
  if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
  return Join-Path $Root $Path
}
function Find-Python {
  $candidates = @((Resolve-Relative $Config.runtimePython))
  foreach ($version in @('311','312','313','314')) {
    $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
  }
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return @{ file=$candidate; prefix=@() } }
  }
  foreach ($name in @('py.exe','python.exe')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
      $prefix = @(); if ($name -eq 'py.exe') { $prefix = @('-3') }
      return @{ file=$command.Source; prefix=$prefix }
    }
  }
  return $null
}
function Get-ServerStatus {
  $tcp = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 8000,20560,28000 })
  $udp = @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 20560,28000 })
  $owners = @(@($tcp) + @($udp) | Select-Object -ExpandProperty OwningProcess -Unique)
  $ready = $false
  if ($owners.Count -eq 1) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($owners[0])" -ErrorAction SilentlyContinue
    $command = if ($process) { [string]$process.CommandLine } else { '' }
    $expectedScript = Resolve-Relative $Config.serverScript
    $localProcess = ($script:ServerProcess -and $script:ServerProcess.Id -eq $owners[0])
    $journalMatch = $false
    $journal = Join-Path $Root 'analysis\current_deployment.json'
    if (Test-Path -LiteralPath $journal) {
      try { $journalMatch = (Get-Content $journal -Raw -Encoding UTF8 | ConvertFrom-Json).pid -eq $owners[0] } catch {}
    }
    $identity = ($process -and $process.Name -in 'python.exe','pythonw.exe' -and
      ($command.Contains($expectedScript) -or $localProcess -or
       ($journalMatch -and $command -match 'server[\\/]run_all\.py')))
    $ready = ($identity -and @($tcp.LocalPort | Select-Object -Unique).Count -eq 3 -and
      @($udp.LocalPort | Select-Object -Unique).Count -eq 2)
  }
  return @{ ready=[bool]$ready; occupied=($owners.Count -gt 0); pid=if ($ready) { $owners[0] } else { 0 } }
}
function Set-Status([string]$Text, [System.Drawing.Color]$Color = [System.Drawing.Color]::LightGray) {
  if($Color -eq [Drawing.Color]::LightGray){$Color=$script:Theme.Ink}
  if($Color -eq [Drawing.Color]::LightGreen){$Color=[Drawing.ColorTranslator]::FromHtml('#93E4C8')}
  if($Color -eq [Drawing.Color]::OrangeRed){$Color=[Drawing.ColorTranslator]::FromHtml('#FFA396')}
  if($Color -eq [Drawing.Color]::Khaki){$Color=$script:Theme.Gold}
  if ($script:StatusLabel) { $script:StatusLabel.Text=$Text; $script:StatusLabel.ForeColor=$Color }
}
function Invoke-RuntimeCheck($Python) {
  $arguments = @($Python.prefix) + @((Join-Path $PSScriptRoot 'preflight.py'))
  $raw = & $Python.file @arguments 2>&1
  if ($LASTEXITCODE -ne 0) { throw "运行依赖检查失败：$raw" }
  $result = ($raw -join "`n") | ConvertFrom-Json
  if (-not $result.ok) { throw "运行依赖检查失败：$($result.error)" }
  return $result
}
function Launch-Game {
  $game = Resolve-Relative $Config.gameExecutable
  if (-not (Test-Path -LiteralPath $game)) { throw '找不到 game\csxy.exe。' }
  $running = @(Get-CimInstance Win32_Process -Filter "Name='csxy.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $game })
  if ($running.Count) { Set-Status '游戏已在运行，请切换到游戏窗口。' ([System.Drawing.Color]::LightGreen); return }
  # The original client validates -redirect with IsWindow before showing its launcher warning.
  # It parses the HWND with 32-bit atoi, so preserve the signed low 32 bits.
  $handleBytes = [System.BitConverter]::GetBytes($form.Handle.ToInt64())
  $launcherHandle = [System.BitConverter]::ToInt32($handleBytes, 0)
  $null = Start-Process -FilePath $game -ArgumentList @('-redirect', $launcherHandle.ToString([System.Globalization.CultureInfo]::InvariantCulture)) -WorkingDirectory (Split-Path $game)
  Set-Status '游戏已启动。在游戏登录界面填写账号和测试密码。' ([System.Drawing.Color]::LightGreen)
}
function Start-Server([bool]$LaunchGame = $false) {
  try {
    if ($script:Busy) { if ($LaunchGame) { $script:GameAfterReady=$true }; return }
    $status = Get-ServerStatus
    if ($status.ready) {
      Set-Status "服务端已运行（PID $($status.pid)）。" ([System.Drawing.Color]::LightGreen)
      if ($LaunchGame) { Launch-Game }; return
    }
    if ($status.occupied) { throw '游戏端口被其他进程占用，可点击服务卡片的「一键清理」释放端口。' }
    if ($script:ServerProcess -and -not $script:ServerProcess.HasExited) {
      throw '服务进程仍在运行但尚未就绪，请查看日志。'
    }
    $python = Find-Python
    if (-not $python) { throw '缺少 Python。请使用含 runtime 的完整分发包。' }
    $script:Busy=$true
    $null = Invoke-RuntimeCheck $python
    $logDir = Join-Path $Root 'server\logs'
    $null = New-Item -ItemType Directory -Path $logDir -Force
    $script:ServerOut = Join-Path $logDir ('launcher_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '.out.log')
    $env:LOGIN_HOST='127.0.0.1'; $env:HTTP_BIND='127.0.0.1'; $env:REPLY_MODE='login_ok'
    $env:HTTP_PORT='8000'; $env:LOGIN_PORT='20560'; $env:BASEAPP_PORT='28000'
    $arguments = @($python.prefix) + @('-u', ('"' + (Resolve-Relative $Config.serverScript) + '"'))
    $script:ServerProcess = Start-Process -FilePath $python.file -ArgumentList $arguments `
      -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $script:ServerOut `
      -RedirectStandardError $script:ServerOut.Replace('.out.log','.err.log') -PassThru
    $script:GameAfterReady=$LaunchGame
    $script:StartupDeadline=(Get-Date).AddSeconds(30)
    Set-Status '服务端启动中，请稍候…' ([System.Drawing.Color]::Khaki)
  } catch { $script:Busy=$false; Set-Status $_.Exception.Message ([System.Drawing.Color]::OrangeRed) }
}
function Poll-Startup {
  if (-not $script:Busy -or -not $script:ServerProcess) { return }
  try {
    if ($script:ServerProcess.HasExited) { throw '服务端启动失败，请查看服务日志。' }
    $readyLine=$false
    if (Test-Path -LiteralPath $script:ServerOut) {
      $readyLine=(Get-Content $script:ServerOut -Raw -Encoding UTF8) -match 'READY: all five listeners are bound; RSA resource key matches'
    }
    if ($readyLine -and (Get-ServerStatus).ready) {
      $script:Busy=$false
      Set-Status '服务端已就绪。' ([System.Drawing.Color]::LightGreen)
      if ($script:GameAfterReady) { Launch-Game }
    } elseif ((Get-Date) -gt $script:StartupDeadline) { throw '启动等待超时，请查看服务日志。' }
  } catch { $script:Busy=$false; Set-Status $_.Exception.Message ([System.Drawing.Color]::OrangeRed) }
}
function Stop-OwnedServer {
  try {
    $status=Get-ServerStatus
    if(-not $status.ready){Set-Status '本目录没有可停止的服务。';return}
    $game = Resolve-Relative $Config.gameExecutable
    if (@(Get-CimInstance Win32_Process -Filter "Name='csxy.exe'" | Where-Object { $_.ExecutablePath -eq $game }).Count) {
      throw '请先退出游戏，再停止服务端。'
    }
    $statePath = Join-Path $Root 'server\state\characters.json'
    if (Test-Path $statePath) {
      $state=Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
      foreach ($account in $state.accounts.PSObject.Properties) {
        foreach ($character in $account.Value) {
          if ($character.world_state.tutorial_battle.phase -in 'preparing','input','animating') {
            throw '存档中仍有进行中的战斗，请重登结束战斗后再停止。'
          }
        }
      }
    }
    Stop-Process -Id $status.pid
    $script:Busy=$false
    Set-Status '服务端已停止。存档已保留。' ([System.Drawing.Color]::Khaki)
  } catch { Set-Status $_.Exception.Message ([System.Drawing.Color]::OrangeRed) }
}
function Open-Path([string]$Path) {
  if (Test-Path -LiteralPath $Path) { $null=Start-Process explorer.exe -ArgumentList ('"'+$Path+'"') }
}
function Open-SaveFolder {
  $path=Join-Path $Root 'server\state';$null=New-Item -ItemType Directory -Path $path -Force;Open-Path $path
}
function Save-CaptureSetting([bool]$Enabled) {
  $Config|Add-Member -NotePropertyName captureEnabled -NotePropertyValue $Enabled -Force
  $temporary=$ConfigPath+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
  try{[IO.File]::WriteAllText($temporary,($Config|ConvertTo-Json -Depth 8),(New-Object Text.UTF8Encoding($true)));Move-Item -LiteralPath $temporary -Destination $ConfigPath -Force}
  finally{if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary}}
}
if ($CheckOnly) {
  $python=Find-Python
  $runtime=$null; if ($python) { $runtime=Invoke-RuntimeCheck $python }
  [pscustomobject]@{game_exists=(Test-Path (Resolve-Relative $Config.gameExecutable));
    python_found=[bool]$python; runtime=$runtime; server_ready=(Get-ServerStatus).ready} | ConvertTo-Json
  exit 0
}

. (Join-Path $PSScriptRoot 'MainWindow.ps1')
