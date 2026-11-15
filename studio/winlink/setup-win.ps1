<#
  Windows 端一键安装：下载 whisper.cpp / 模型 / ffmpeg / 内嵌 Python，写好 config.json，
  注册开机自启（计划任务 ASRReceiver）。

  用法（普通 PowerShell，不需要管理员；写成计划任务那步需要管理员）：
    powershell -ExecutionPolicy Bypass -File setup-win.ps1
    powershell -ExecutionPolicy Bypass -File setup-win.ps1 -Summary ollama -TaskName ASRReceiver
    powershell -ExecutionPolicy Bypass -File setup-win.ps1 -Cpu          # 显卡那版跑不起来时强制 CPU
    powershell -ExecutionPolicy Bypass -File setup-win.ps1 -DryRun       # 只看会做什么
#>
param(
  [string]$DataDir = "C:\ASR",
  [string]$Summary = "gemini",          # gemini | ollama | deepseek | none
  [string]$SummaryModel = "",
  [string]$SummaryKey = "",
  [string]$OllamaUrl = "http://127.0.0.1:11434",
  [string]$TaskName = "ASRReceiver",
  [int]$Threads = 8,
  [int]$SignalPort = 8765,
  [switch]$Cpu,
  [string]$WhisperBin = "",    # 已经装好 whisper-cli.exe 的话，直接给路径就跳过下载
  [string]$ModelPath = "",     # 已经有 ggml 模型的话，直接给路径
  [string]$FfmpegExe = "",     # 已经有 ffmpeg.exe 的话，直接给路径
  [switch]$WithOllama,
  [string]$OllamaModel = "qwen3:14b",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[setup] $m" }

$Urls = @{
  python  = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
  gpu     = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-win-cuda-12.4.0-x64.zip"
  cpu     = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip"
  model   = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
  ffmpeg  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
}

function Get-File($url, $dest) {
  if (Test-Path $dest) { Say "已存在，跳过：$(Split-Path $dest -Leaf)"; return }
  Say "下载 $(Split-Path $dest -Leaf) …"
  if ($DryRun) { return }
  if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    & curl.exe -L --fail --retry 3 -o $dest $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败：$url" }
  } else {
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
  }
}

function Expand-Zip($zip, $dest) {
  if ($DryRun) { Say "解压 $zip → $dest"; return }
  if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
  Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
}

# ---------- 找 OneDrive 里的 ASR 目录 ----------
if (-not $env:USERPROFILE) { $env:USERPROFILE = $HOME }   # 只为在本机 dry-run 时能跑

function Find-AsrRoot {
  $cands = @()
  Get-ChildItem $env:USERPROFILE -Directory -Filter "OneDrive*Waterloo*" -ErrorAction SilentlyContinue |
    ForEach-Object { $cands += (Join-Path $_.FullName "ASR") }
  $cands += (Join-Path $env:USERPROFILE "OneDrive\ASR")
  Get-ChildItem $env:USERPROFILE -Directory -Filter "OneDrive*" -ErrorAction SilentlyContinue |
    ForEach-Object { $cands += (Join-Path $_.FullName "ASR") }
  $cands += (Join-Path $env:USERPROFILE "OneDrive - 个人\ASR")
  foreach ($c in $cands) { if (Test-Path $c) { return $c } }
  return (Join-Path $env:USERPROFILE "OneDrive\ASR")
}

$AsrRoot = Find-AsrRoot
Say "OneDrive 任务目录：$AsrRoot"
$pyDir   = Join-Path $DataDir "python"
$binDir  = Join-Path $DataDir "bin"
$ffDir   = Join-Path $DataDir "ffmpeg"
$modDir  = Join-Path $DataDir "models"
$dlDir   = Join-Path $DataDir "downloads"

if (-not $DryRun) {
  foreach ($d in @($DataDir, $pyDir, $binDir, $modDir, $dlDir, $AsrRoot,
                   (Join-Path $AsrRoot "inbox"), (Join-Path $AsrRoot "out"))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
  }
}

# ---------- 1. 内嵌 Python ----------
$pyExe = Join-Path $pyDir "python.exe"
if (-not (Test-Path $pyExe)) {
  $z = Join-Path $dlDir "python-embed.zip"
  Get-File $Urls.python $z
  Expand-Zip $z $pyDir
}
Say "Python：$pyExe"

# ---------- 2. whisper.cpp ----------
$whisperExe = Join-Path $binDir "whisper-cli.exe"
if ($WhisperBin) {
  if (-not (Test-Path $WhisperBin)) { throw "给的 whisper 路径不存在：$WhisperBin" }
  $whisperExe = (Resolve-Path $WhisperBin).Path
  Say "用你已有的 whisper：$whisperExe"
} elseif (Test-Path $whisperExe) {
  Say "whisper 已在 $whisperExe，跳过下载"
} else {
  $want = if ($Cpu) { $Urls.cpu } else { $Urls.gpu }
  $whisperZip = Join-Path $dlDir (Split-Path $want -Leaf)
  Get-File $want $whisperZip
  Expand-Zip $whisperZip $binDir
  # 压缩包里通常直接是 exe 和 dll；若多套了一层目录就搬上来
  $inner = Get-ChildItem $binDir -Recurse -Filter "whisper-cli.exe" -ErrorAction SilentlyContinue |
           Select-Object -First 1
  if ($null -eq $inner) { if (-not $DryRun) { throw "压缩包里没找到 whisper-cli.exe" } }
  if ($inner -and $inner.DirectoryName -ne $binDir) {
    Copy-Item (Join-Path $inner.DirectoryName "*") $binDir -Recurse -Force
  }
}
Say "whisper：$whisperExe"
if (-not $DryRun) {
  $out = & $whisperExe --help 2>&1
  if ($LASTEXITCODE -eq 0 -or ($out -join " ") -match "usage") {
    Say "whisper 能启动"
  } else {
    Say "⚠ whisper 起不来（多半缺 CUDA 运行库或驱动太旧）：重跑时加 -Cpu"
  }
}

# ---------- 3. 模型 ----------
$modelFile = Join-Path $modDir "ggml-large-v3-turbo.bin"
if ($ModelPath) {
  if (-not (Test-Path $ModelPath)) { throw "给的模型路径不存在：$ModelPath" }
  $src = (Resolve-Path $ModelPath).Path
  if (-not $DryRun -and $src -ne $modelFile) { Copy-Item $src $modelFile -Force }
  Say "用你已有的模型：$src"
} elseif (Test-Path $modelFile) {
  Say "模型已在 $modelFile，跳过下载"
} else {
  Get-File $Urls.model $modelFile
}

# ---------- 4. ffmpeg ----------
$ffExe = Join-Path $ffDir "bin\ffmpeg.exe"
if ($FfmpegExe) {
  if (-not (Test-Path $FfmpegExe)) { throw "给的 ffmpeg 路径不存在：$FfmpegExe" }
  $ffExe = (Resolve-Path $FfmpegExe).Path
  Say "用你已有的 ffmpeg：$ffExe"
} elseif (-not (Test-Path $ffExe)) {
  $fz = Join-Path $dlDir "ffmpeg.zip"
  Get-File $Urls.ffmpeg $fz
  Expand-Zip $fz $ffDir
  $inner = Get-ChildItem $ffDir -Recurse -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue |
           Select-Object -First 1
  if ($null -eq $inner) { if (-not $DryRun) { throw "ffmpeg 解压后找不到 ffmpeg.exe" } }
  if ($inner -and $inner.DirectoryName -ne (Split-Path $ffExe -Parent)) {
    $target = Join-Path $ffDir "bin"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item $inner.FullName (Join-Path $target "ffmpeg.exe") -Force
    $ffm = Get-ChildItem $inner.DirectoryName -Filter "ffprobe.exe" -ErrorAction SilentlyContinue
    if ($ffm) { Copy-Item $ffm.FullName (Join-Path $target "ffprobe.exe") -Force }
  }
}
Say "ffmpeg：$ffExe"

# ---------- 5. receiver.py ----------
$srcRecv = Join-Path $PSScriptRoot "receiver.py"
$dstRecv = Join-Path $DataDir "receiver.py"
if (Test-Path $srcRecv) {
  if (-not $DryRun) { Copy-Item $srcRecv $dstRecv -Force }
  Say "receiver.py → $dstRecv"
} else {
  Say "⚠ 同目录下没找到 receiver.py，去 $AsrRoot\tools 里找找"
}

# ---------- 6. config.json ----------
$cfg = [ordered]@{
  root       = $AsrRoot
  whisper_bin = $whisperExe
  model      = $modelFile
  ffmpeg     = $ffExe
  threads    = $Threads
  summary    = [ordered]@{ provider = $Summary; url = $OllamaUrl }
  signal     = [ordered]@{ port = $SignalPort; token = "asr-9c41f7b2" }
}
switch ($Summary) {
  "ollama"   { $cfg.summary.model = $(if ($SummaryModel) { $SummaryModel } else { "qwen3:14b" }) }
  "gemini"   { $cfg.summary.model = $(if ($SummaryModel) { $SummaryModel } else { "gemini-2.5-flash" })
               $cfg.summary.key = $SummaryKey }
  "deepseek" { $cfg.summary.model = $(if ($SummaryModel) { $SummaryModel } else { "deepseek-chat" })
               $cfg.summary.key = $SummaryKey }
  default    { }
}
$cfgPath = Join-Path $DataDir "config.json"
if (-not $DryRun) {
  $cfg | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $cfgPath
}
Say "配置：$cfgPath（总结用 $Summary）"

# ---------- 6.5 本地总结模型（可选） ----------
if ($WithOllama) {
  if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Say "Ollama 已安装"
  } elseif ($DryRun) {
    Say "会安装 Ollama"
  } else {
    Say "安装 Ollama …"
    winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
    Start-Sleep -Seconds 5
  }
  if (-not $DryRun) {
    Say "拉取模型 $OllamaModel（几个 GB，慢一点）…"
    & ollama pull $OllamaModel
  }
}

# ---------- 7. 开机自启 ----------
if (-not $DryRun) {
  $pw = Join-Path $pyDir "pythonw.exe"
  if (-not (Test-Path $pw)) { $pw = $pyExe }
  $arg = "`"$dstRecv`" --config `"$cfgPath`" --listen $SignalPort"
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
  try {
    $action = New-ScheduledTaskAction -Execute $pw -Argument $arg -WorkingDirectory $DataDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -RunLevel Highest -Force | Out-Null
    Say "已注册计划任务 $TaskName（登录自启）"
  } catch {
    # 没有管理员权限就退回「启动文件夹」快捷方式
    $startup = [Environment]::GetFolderPath("Startup")
    $lnk = Join-Path $startup "ASRReceiver.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $pw
    $sc.Arguments = $arg
    $sc.WorkingDirectory = $DataDir
    $sc.WindowStyle = 7
    $sc.Save()
    Say "没有管理员权限，已改为启动文件夹快捷方式：$lnk"
  }
  # 防火墙：只放行 Tailscale 网段（100.64.0.0/10）
  try {
    if (-not (Get-NetFirewallRule -DisplayName "ASR signal $SignalPort" -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName "ASR signal $SignalPort" -Direction Inbound -Protocol TCP `
        -LocalPort $SignalPort -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
      Say "已加防火墙规则（只允许 Tailscale 网段访问 $SignalPort）"
    }
  } catch { Say "⚠ 防火墙规则没加成（多半没管理员权限），Mac 到 Windows 的信号可能被拦" }

  Start-Process -FilePath $pw -ArgumentList $arg -WorkingDirectory $DataDir -WindowStyle Hidden
  Say "已在后台启动接收端"
}

# ---------- 8. 自查 ----------
Say ""
Say "=== 自查 ==="
Say "OneDrive 任务目录 : $AsrRoot"
Say "whisper          : $whisperExe"
Say "模型             : $modelFile"
Say "ffmpeg           : $ffExe"
Say "接收端           : $dstRecv"
Say "配置             : $cfgPath"
Say "总结             : $Summary"
Say ""
Say "把音频拖到 Mac 的「发到 Windows」上，几十秒后 $AsrRoot\out 里就会出现结果。"
