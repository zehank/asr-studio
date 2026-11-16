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
  [switch]$EnableSsh,
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

# ---------- 7. SSH + 交互式工作任务 ----------
# OneDrive 的「按需下载」文件只有用户会话能读；SSH 落在 session 0，读不动云文件。
# 所以：SSH 只负责 /run 一个交互式计划任务，真正的处理在用户会话里跑。
$PubKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICDeV6+tthAqKMG9KDKScFCOwEw8AMdiGD38beYum8xq zehan@mac'
if (-not $DryRun) {
  $pyw = Join-Path $pyDir "python.exe"
  $argText = "`"$dstRecv`" --config `"$cfgPath`" --once"
  $action = New-ScheduledTaskAction -Execute $pyw -Argument $argText -WorkingDirectory $DataDir
  $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Highest
  Register-ScheduledTask -TaskName "ASRWorker" -Action $action -Principal $principal -Force | Out-Null
  Say "已注册交互式任务 ASRWorker（没有触发器，只在 Mac 叫它时跑）"
}

if ($EnableSsh) {
  if ($DryRun) {
    Say "会安装并启用 OpenSSH Server，并把 Mac 的公钥加进 administrators_authorized_keys"
  } else {
    $cap = Get-WindowsCapability -Online -Name OpenSSH.Server* | Select-Object -First 1
    if (-not $cap -or $cap.State -ne "Installed") {
      Say "安装 OpenSSH Server …"
      Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    }
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    $f = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"
    if (-not (Test-Path $f)) { New-Item -ItemType File -Path $f | Out-Null }
    if (-not (Select-String -Path $f -SimpleMatch "zehan@mac" -Quiet -ErrorAction SilentlyContinue)) {
      Add-Content $f $PubKey
    }
    icacls $f /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
    Say "sshd 已启动并设为自动；Mac 的公钥已加入"
  }
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
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
Say "sshd             : $(if ($sshd) { $sshd.Status } else { '未安装（加 -EnableSsh 可装）' })"
Say ""
Say "把音频拖到 Mac 的「发到 Windows」上，几十秒后 $AsrRoot\out 里就会出现结果。"
