# Windows 端安装（SSH 触发 + OneDrive 传文件）

Mac 端：拖文件 → 写进 `OneDrive - University of Waterloo\ASR\inbox\<task-id>\` → 用 **SSH** 叫 Windows 处理。
Windows 端：收到 ssh 命令就跑一次 `receiver.py --once`，干完写 `ASR\out\<task-id>\`。
**没有常驻服务、没有开机自启、没有监听端口。**

## 一条命令装完

管理员 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\OneDrive - University of Waterloo\ASR\tools\setup-win.ps1" -EnableSsh -Summary ollama
```

它会：

1. 建 `C:\ASR\{python,bin,models,ffmpeg,downloads}`
2. 下载内嵌 Python 3.12（解压即用，不动系统 Python）
3. 下载 whisper.cpp CUDA 版 → `C:\ASR\bin\whisper-cli.exe`（自带 cublas/cudart，不用装 CUDA）
4. 下载 `ggml-large-v3-turbo.bin`（1.5 GB）
5. 下载 ffmpeg → `C:\ASR\ffmpeg\bin\ffmpeg.exe`
6. 写 `C:\ASR\config.json`
7. `-EnableSsh` 时：装 OpenSSH Server、设为自动启动、把 Mac 的公钥写进 `administrators_authorized_keys`

约 2.5 GB 下载。总结模型 `-Summary` 可选 `ollama` / `gemini` / `deepseek` / `none`。

## 用自己的模型（跳过下载）

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\OneDrive - University of Waterloo\ASR\tools\setup-win.ps1" -EnableSsh `
  -WhisperBin "C:\你的路径\whisper-cli.exe" `
  -ModelPath  "C:\你的路径\ggml-large-v3-turbo.bin" `
  -Summary ollama
```

必须是 whisper.cpp 的 `whisper-cli.exe` + ggml 模型（不是 Python 的 `openai-whisper` 包）。

## 验收

Windows 本机手动跑一次：

```powershell
C:\ASR\python\python.exe C:\ASR\receiver.py --config C:\ASR\config.json --once
```

Mac 上验证通道：

```bash
ssh rzhank@100.77.101.77 "echo ok"
```

Mac 上拖一个文件到「发到 Windows」，几秒内 Windows 的 `C:\ASR` 就会忙起来，完成后 `ASR\out\<task-id>\` 出现：

- `<名字>.txt`、`<名字>.srt`
- `<名字>.translation.txt`（选了翻译）
- `<名字>.summary.md`（选了总结）
- `status.json`：`state=done` / `failed`（失败带 `error`）

## 排错

| 现象 | 原因 |
|---|---|
| Mac 上 `trigger: failed — connect to host … timed out` | Windows 没开 sshd，或 Tailscale 没连上 |
| `Permission denied (publickey)` | 公钥没写进 `administrators_authorized_keys`，或那条 ACL 不对（只能 Administrators + SYSTEM 有权限） |
| `status.json` 里 `state=failed` | 看 `error`：多半是模型路径或显卡运行库 |
| 一直 `等同步` | OneDrive 还没下完文件；接收端要求 `job.json` ≥5 秒且文件大小一致 |

## 关掉 SSH（想停整套流水线时）

```powershell
Stop-Service sshd
Set-Service sshd -StartupType Disabled
```

彻底卸载：`Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`
