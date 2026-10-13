$form=New-Object CSXY.UI.Window
$form.Text='创世西游 · 单机启动器 V1.1';$form.StartPosition='CenterScreen'
$form.ClientSize=New-Object Drawing.Size(1040,690);$form.FormBorderStyle='FixedSingle';$form.MaximizeBox=$false
$form.AutoScaleMode='Dpi';$form.BackColor=$script:Theme.Background;$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$rail=New-Object Windows.Forms.Panel;$rail.Location=New-Object Drawing.Point(0,0);$rail.Size=New-Object Drawing.Size(96,690)
$rail.BackColor=$script:Theme.Rail;$form.Controls.Add($rail)
$logo=New-Text $rail '西游' 16 22 66 38 20 $true;$logo.TextAlign='MiddleCenter'
$null=New-Text $rail 'CSXY' 23 62 58 24 9 $true $script:Theme.Muted
$navItems=@(@('◈','首页', {Start-Server $true}),@('✦','GM',{Show-GMTools}),@('▣','存档',{Open-SaveFolder}),@('≡','日志',{Open-Path (Join-Path $Root 'server\logs')}),@('?','帮助',{Show-HelpWindow}))
for($i=0;$i -lt $navItems.Count;$i++){
  $item=$navItems[$i];$top=130+$i*93
  $button=New-Action $rail $item[0] 19 $top 58 47 $item[2] ($i -eq 0)
  if($i -ne 0){$button.FillColor=$script:Theme.Rail};$button.Font=New-Object Drawing.Font('Segoe UI Symbol',21)
  $label=New-Text $rail $item[1] 15 ($top+52) 66 23 9;$label.TextAlign='MiddleCenter'
}
$null=New-Text $rail 'V1.1' 21 642 66 22 9 $false $script:Theme.Muted
$null=New-Text $form '创世西游' 128 27 240 38 22 $true
$null=New-Text $form '单机控制台 V1.1  /  自在启程' 128 70 430 24 10 $false $script:Theme.Muted
$null=New-Action $form '更新中心  ↗' 624 40 203 39 {Show-UpdateWindow}
$badge=New-Card $form 863 34 145 35 ([Drawing.ColorTranslator]::FromHtml('#38436A'))
$tag=New-Text $badge '●  本机 · 独立存档' 10 8 128 24 9 $false ([Drawing.ColorTranslator]::FromHtml('#93E4C8'))
$dark=[Drawing.ColorTranslator]::FromHtml('#554522')
$gameCard=New-Card $form 128 116 280 177 $script:Theme.Gold ([Drawing.ColorTranslator]::FromHtml('#F6B92C')) $true
$null=New-Text $gameCard '开始你的西游' 20 20 250 30 12 $true $dark
$null=New-Text $gameCard '一键开玩' 20 60 240 42 25 $true $dark
$play=New-Action $gameCard '启动服务并进入游戏    →' 20 120 240 36 {Start-Server $true}
$play.FillColor=[Drawing.ColorTranslator]::FromHtml('#FFE18B');$play.ForeColor=$dark
$serverCard=New-Card $form 428 116 280 177 ([Drawing.ColorTranslator]::FromHtml('#FF9270')) ([Drawing.ColorTranslator]::FromHtml('#F6795C')) $true
$coralInk=[Drawing.ColorTranslator]::FromHtml('#59312A')
$null=New-Text $serverCard '本地服务端' 20 20 230 28 12 $true $coralInk
$script:ServiceValue=New-Text $serverCard '等待启动' 20 63 240 38 22 $true $coralInk
$null=New-Text $serverCard '游戏所需服务，由启动器管理' 20 126 246 24 10 $false $coralInk
$gmCard=New-Card $form 728 116 280 177 ([Drawing.ColorTranslator]::FromHtml('#7545F2')) ([Drawing.ColorTranslator]::FromHtml('#6533DF')) $true
$null=New-Text $gmCard 'GM 工具' 20 20 240 28 12 $true
$null=New-Text $gmCard '角色存档' 20 62 240 39 23 $true
$gmButton=New-Action $gmCard '等级 / 金钱 / 元宝    →' 20 120 240 36 {Show-GMTools}
$gmButton.FillColor=[Drawing.ColorTranslator]::FromHtml('#8C66F5')
$capture=New-Card $form 128 318 240 113
$null=New-Text $capture '协议抓包' 19 19 145 28 12 $true
$captureToggle=New-Object CSXY.UI.Toggle;$captureToggle.Size=New-Object Drawing.Size(48,26)
$captureToggle.Location=New-Object Drawing.Point(172,21);$captureToggle.Checked=$Config.captureEnabled -eq $true;$captureToggle.AccessibleName='协议抓包开关'
$capture.Controls.Add($captureToggle)
$captureState=New-Text $capture $(if($captureToggle.Checked){'已开启'}else{'已关闭'}) 20 58 180 26 13 $true
$null=New-Text $capture '仅排查问题时开启' 20 86 205 23 8 $false $script:Theme.Muted
$script:UpdatingCapture=$false
$captureToggle.Add_CheckedChanged({
  if($script:UpdatingCapture){return}
  $previous=$Config.captureEnabled -eq $true
  try{
    Save-CaptureSetting $captureToggle.Checked
    $captureState.Text=if($captureToggle.Checked){'已开启'}else{'已关闭'}
    Set-Status $(if($captureToggle.Checked){'抓包已开启，记录保存到 server\evidence。'}else{'抓包已关闭。已有记录保留；游戏与服务继续正常运行。'})
  }catch{
    $script:UpdatingCapture=$true;$captureToggle.Checked=$previous;$Config.captureEnabled=$previous;$script:UpdatingCapture=$false
    Set-Status $_.Exception.Message ([Drawing.Color]::OrangeRed)
  }
})
$saveCard=New-Card $form 128 449 240 127
$null=New-Text $saveCard '独立存档' 20 18 200 27 12 $true
$null=New-Text $saveCard '你的旅程，本机保存' 20 56 213 27 11
$null=New-Text $saveCard 'GM 修改前自动生成备份' 20 94 213 23 8 $false $script:Theme.Muted
$workspace=New-Card $form 392 318 616 258
$null=New-Text $workspace '常用工具' 24 20 390 28 13 $true
$toolItems=@(@('▣','存档目录',{Open-SaveFolder}),@('≡','服务日志',{Open-Path (Join-Path $Root 'server\logs')}),@('✎','怪物编辑',{}),@('?','使用说明',{Show-HelpWindow}))
for($i=0;$i -lt $toolItems.Count;$i++){
  $item=$toolItems[$i];$x=33+$i*145
  $button=New-Action $workspace $item[0] ($x+21) 68 64 64 $item[2]
  $button.Circle=$true;$button.FillColor=[Drawing.ColorTranslator]::FromHtml('#434D7C');$button.Font=New-Object Drawing.Font('Segoe UI Symbol',25)
  if($i -eq 2){$button.Enabled=$false}
  $label=New-Text $workspace $item[1] $x 144 111 24 10;$label.TextAlign='MiddleCenter'
  if($i -eq 2){$hint=New-Text $workspace '待开放' $x 170 111 20 8 $false $script:Theme.Muted;$hint.TextAlign='MiddleCenter'}
}
$line=New-Object Windows.Forms.Panel;$line.Location=New-Object Drawing.Point(24,201);$line.Size=New-Object Drawing.Size(568,1);$line.BackColor=$script:Theme.Line;$workspace.Controls.Add($line)
$script:StatusLabel=New-Text $workspace '点击「一键开玩」，服务就绪后自动打开游戏。' 24 214 568 40 9 $false $script:Theme.Muted
$null=New-Action $form '启动服务端' 128 603 280 46 {Start-Server}
$null=New-Action $form '启动游戏' 428 603 280 46 {Start-Server $true} $true
$stop=New-Action $form '停止服务端' 728 603 280 46 {Stop-OwnedServer};$stop.FillColor=[Drawing.ColorTranslator]::FromHtml('#414B7B')
$null=New-Text $form '关闭启动器后，游戏和服务会继续运行。' 128 664 680 22 8 $false $script:Theme.Muted
function Update-DashboardStatus {
  $status=Get-ServerStatus
  $script:ServiceValue.Text=if($script:Busy){'正在启动…'}elseif($status.ready){'正在运行'}elseif($status.occupied){'端口被占用'}else{'已停止'}
}
Add-WindowChrome $form '创世西游 / 单机启动器 V1.1'
$script:DashboardTicks=0
$timer=New-Object Windows.Forms.Timer;$timer.Interval=1500
$timer.Add_Tick({Poll-Startup;$script:DashboardTicks++;if($script:Busy -or $script:DashboardTicks%5 -eq 0){Update-DashboardStatus}});$timer.Start()
$form.Add_FormClosing({$timer.Stop();$timer.Dispose()})
$form.Add_Shown({Update-DashboardStatus;if($Config.autoStartServer){Start-Server}})
if($UpdatePreviewPath){Show-UpdateWindow $UpdatePreviewPath;$timer.Dispose();$form.Dispose();exit 0}
if($GMPreviewPath){Show-GMTools $GMPreviewPath;$timer.Dispose();$form.Dispose();exit 0}
if($PreviewPath){Save-FormPreview $form $PreviewPath;$timer.Dispose();$form.Dispose();exit 0}
[void]$form.ShowDialog()