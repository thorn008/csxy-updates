function Show-UpdateWindow([string]$PreviewPath=''){
  $window=New-Object CSXY.UI.Window;$window.Text='创世西游 · 更新中心';$window.StartPosition='CenterParent'
  $window.ClientSize=New-Object Drawing.Size(820,690);$window.MaximizeBox=$false
  $window.AutoScaleMode='Dpi';$window.BackColor=$script:Theme.Background;$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
  $null=New-Text $window '更新中心' 30 22 700 44 24 $true
  $subtitle=New-Text $window '启动器 V1.1  /  保留你的存档与设置' 32 77 720 25 10 $false $script:Theme.Muted
  $card=New-Card $window 30 118 760 176
  $null=New-Text $card 'GitHub 更新清单地址（HTTPS）' 22 18 700 26 11 $true
  $url=New-Object Windows.Forms.TextBox;$url.Location=New-Object Drawing.Point(22,57);$url.Size=New-Object Drawing.Size(716,30)
  $url.BackColor=$script:Theme.Background;$url.ForeColor=$script:Theme.Ink;$url.BorderStyle='FixedSingle';$card.Controls.Add($url)
  $online=New-Action $card '保存地址并检查更新' 22 108 239 40 {} $true
  $local=New-Action $card '选择本地补丁文件夹' 278 108 239 40 {}
  $details=New-Card $window 30 314 760 256
  $notesHeading=New-Text $details '本次更新内容' 22 16 700 28 12 $true
  $document=New-Object CSXY.UI.DocumentView;$document.Location=New-Object Drawing.Point(22,56);$document.Size=New-Object Drawing.Size(716,180)
  $document.BackColor=$script:Theme.Card;$document.ForeColor=$script:Theme.Ink;$document.Font=$window.Font
  $document.Text="已预置官方 GitHub 更新地址，也可选择本地补丁更新。`r`n`r`n先检查补丁，再点击安装。安装前请退出游戏并停止服务端。`r`n更新后关闭并重新打开启动器。旧版启动器仍可正常游玩。"
  $details.Controls.Add($document)
  $message=New-Text $window '' 32 588 540 69 9
  $install=New-Action $window '安装已检查的补丁' 585 598 205 43 {} $true;$install.Enabled=$false
  $context=@{source='';remote=$false;token='';process=$null;pending='';url='';applied=$false;notes='';version=''}
  $python=Find-Python;$backendPath=Join-Path $PSScriptRoot 'update_cli.py';$workingDirectory=$Root
  $backendFunction=${function:Invoke-GMBackend}
  $callBackend={param($request)& $backendFunction $request $python $backendPath $workingDirectory}.GetNewClosure()
  try{
    $settings=& $callBackend @{action='settings'}
    $url.Text=$settings.url;$subtitle.Text='启动器 V1.1  /  当前内容版本 '+$settings.installed.releaseVersion
    $notesPath=Join-Path $Root 'server\data\release_notes.json'
    if(Test-Path -LiteralPath $notesPath){
      try{
        $release=Get-Content -LiteralPath $notesPath -Raw -Encoding UTF8|ConvertFrom-Json
        if($release.version -eq $settings.installed.releaseVersion){
          $notesHeading.Text='当前版本更新内容 · '+$release.version
          $document.Text=([string]$release.notes -replace '\r?\n',"`r`n")
        }
      }catch{} # Old packages or missing notes retain the normal instructions.
    }
  }catch{$message.Text=$_.Exception.Message}
  # Run downloads in a separate process; timer polls without blocking the UI.
  $startJob={param($request,$pending)
    if(-not $python){throw '缺少 Python 运行环境。'}
    $start=New-Object Diagnostics.ProcessStartInfo;$start.FileName=$python.file
    $start.Arguments=(@($python.prefix)-join ' ')+' -X utf8 "'+$backendPath+'"';$start.WorkingDirectory=$workingDirectory
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.RedirectStandardInput=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$start.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
    $p=New-Object Diagnostics.Process;$p.StartInfo=$start
    try{
      $null=$p.Start();$p.StandardInput.Write(($request|ConvertTo-Json -Depth 8 -Compress));$p.StandardInput.Close()
      $context.stdout=$p.StandardOutput.ReadToEndAsync();$context.stderr=$p.StandardError.ReadToEndAsync()
      $context.process=$p;$context.pending=$pending;$online.Enabled=$false;$local.Enabled=$false;$install.Enabled=$false;$url.Enabled=$false
      $message.Text=if($pending -eq 'apply'){'正在校验、备份并安装，请稍候…'}else{'正在检查补丁，请稍候…'}
    }catch{$p.Dispose();throw}
  }.GetNewClosure()
  $online.Add_Click({
    try{
      if([string]::IsNullOrWhiteSpace($url.Text)){throw '请填写已发布的 GitHub 更新清单地址。'}
      $context.token='';$install.Enabled=$false
      $saved=& $callBackend @{action='save_source';url=$url.Text.Trim()}
      $context.source=$saved.url;$context.remote=$true
      & $startJob @{action='inspect';source=$context.source;remote=$true} 'inspect'
    }catch{$message.Text=$_.Exception.Message}
  }.GetNewClosure())
  $local.Add_Click({
    $picker=New-Object Windows.Forms.FolderBrowserDialog;$picker.Description='选择包含 update-manifest.json 的补丁文件夹';$picker.ShowNewFolderButton=$false
    try{
      if($picker.ShowDialog($window) -eq [Windows.Forms.DialogResult]::OK){
        $context.token='';$install.Enabled=$false;$context.source=$picker.SelectedPath;$context.remote=$false
        & $startJob @{action='inspect';source=$context.source;remote=$false} 'inspect'
      }
    }catch{$message.Text=$_.Exception.Message}finally{$picker.Dispose()}
  }.GetNewClosure())
  $install.Add_Click({
    try{& $startJob @{action='apply';source=$context.source;remote=$context.remote;expected=$context.token} 'apply'}
    catch{$message.Text=$_.Exception.Message}
  }.GetNewClosure())
  $timer=New-Object Windows.Forms.Timer;$timer.Interval=250
  $timer.Add_Tick({
    $p=$context.process
    if(-not $p -or -not $p.HasExited -or -not $context.stdout.IsCompleted -or -not $context.stderr.IsCompleted){return}
    try{
      $result=$context.stdout.Result|ConvertFrom-Json
      if(-not $result.ok){throw $result.error}
      if($context.pending -eq 'inspect'){
        $data=$result.result;$context.token=$data.token
        $context.notes=if([string]::IsNullOrWhiteSpace([string]$data.notes)){'此补丁未提供更新说明。'}else{[string]$data.notes -replace '\r?\n',"`r`n"}
        $context.version=$data.version;$notesHeading.Text='本次更新内容 · '+$data.version
        $document.Text="版本：$($data.current) → $($data.version)  /  $($data.count) 个文件`r`n`r`n$($context.notes)"
        if($data.available -eq $false){
          $context.token='';$notesHeading.Text='远程版本说明 · '+$data.version
          $document.Text="本机版本：$($data.current)  /  远程版本：$($data.version)`r`n`r`n$($context.notes)"
          $message.Text=if($data.current -eq $data.version){'当前已是最新版本，无需安装。'}else{'仓库版本低于本机版本，无需安装。发布者需上传新的补丁。'}
        }else{$message.Text='检查通过。退出游戏并停止服务端后，可以安装。'}
      }else{
        $context.applied=$true;$context.token='';$notesHeading.Text='已更新内容 · '+$result.result.version
        $document.Text="$($context.notes)`r`n`r`n原文件备份：`r`n$($result.result.backup)"
        $subtitle.Text='启动器 V1.1  /  当前内容版本 '+$result.result.version
        $message.Text='更新完成。请关闭整个启动器并重新打开，再启动游戏。'
      }
    }catch{
      $context.token='';$message.Text=$_.Exception.Message;$notesHeading.Text='更新未完成'
      $document.Text=$_.Exception.Message
    }
    finally{$p.Dispose();$context.process=$null;$online.Enabled=-not $context.applied;$local.Enabled=-not $context.applied;$url.Enabled=-not $context.applied;$install.Enabled=[bool]$context.token}
  }.GetNewClosure())
  $window.Add_FormClosing({param($sender,$event)
    if($context.process){$event.Cancel=$true;$message.Text='正在处理更新，请等待操作结束后关闭。'}
  }.GetNewClosure())
  Add-WindowChrome $window '创世西游 / 更新中心 · V1.1' $false
  $timer.Start()
  try{if($PreviewPath){Save-FormPreview $window $PreviewPath}else{[void]$window.ShowDialog($form)}}finally{$timer.Stop();$timer.Dispose();$window.Dispose()}
  if($context.applied -and -not $PreviewPath){$form.Close()}
}
