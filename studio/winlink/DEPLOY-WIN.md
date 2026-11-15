# Windows 端：装接收端（模型你自己装，这里只做适配）

**文件走 Waterloo OneDrive，信号走 Tailscale。**
文件：`OneDrive - University of Waterloo\ASR\{inbox,out,tools}`（Mac 投放器已指向这里）
信号：Mac 投完立刻 POST 到 `http://100.77.101.77:8765/job`（带 token），Windows 上的接收端收到就马上扫，不用等轮询。
信号不通也不影响：接收端还有兜底轮询（默认 60 秒一次；一旦发现有待办就降到 4 秒）。

## 情况 A：你的模型已经有了（自己装好了 whisper）

在 Windows 上，管理员 PowerShell，把三个路径换成你自己的：

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\OneDrive - University of Waterloo\ASR\tools\setup-win.ps1" `
  -WhisperBin "C:\你的路径\whisper-cli.exe" `
  -ModelPath  "C:\你的路径\ggml-large-v3-turbo.bin" `
  -Summary ollama
```

- `-WhisperBin` / `-ModelPath` 给了就**完全不下载模型**，直接复用你的
- 模型不用换名字，脚本只记路径
- 没给 `-FfmpegExe` 的话会下个 ffmpeg（几十 MB，用来把 m4a/mp4 转 16k wav）

## 情况 B：什么都没装，让它一把装齐

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\OneDrive - University of Waterloo\ASR\tools\setup-win.ps1" -Summary ollama -WithOllama -OllamaModel qwen3:14b
```

它会装：内嵌 Python（不污染系统）→ whisper.cpp CUDA 版（自带 cublas/cudart，不用装 CUDA）→ large-v3-turbo 模型 → ffmpeg → Ollama + qwen3:14b，最后注册开机自启。

## 脚本会做什么

1. 建 `C:\ASR\{python,bin,models,ffmpeg,downloads}`
2. 准备 Python（解压即用的内嵌版）
3. 准备 whisper + 模型（或复用你给的路径）
4. 准备 ffmpeg
5. 写 `C:\ASR\config.json`，其中 `root` 自动认到 `OneDrive - University of Waterloo\ASR`
6. 写 `C:\ASR\receiver.py`
7. 注册计划任务 `ASRReceiver`（登录自启；没管理员权限自动退回启动文件夹快捷方式）
8. 打印自查清单

## 验收

Mac 上拖一个短音频到「发到 Windows」→ 选任务 → 等 OneDrive 同步 → 看 `ASR\out\<任务名>\`：

- `<名字>.txt`、`<名字>.srt`
- `<名字>.translation.txt`（选了翻译）
- `<名字>.summary.md`（选了总结）
- `status.json`：`state=done` / `failed`（失败带 `error`）

现在 inbox 里已经躺着一个你之前投的真实录音（`source-标准录音 7.mp3`，102 MB），
接收端装好并自启后它会自动开始处理，结果出现在 `ASR\out\` 下。

## 信号端口

- 端口 8765，token 写在 `config.json` 的 `signal` 里（默认 `asr-9c41f7b2`）
- 安装脚本会自动加防火墙规则，**只放行 Tailscale 网段 100.64.0.0/10**
- 自检：`curl http://100.77.101.77:8765/` 应返回 `{"ok": true, "service": "asr-signal"}`

## 手动跑一次（排查用）

```powershell
cd C:\ASR
.\python\python.exe receiver.py --config config.json --once
```

## 坑

- 半同步的文件会跳过：要求 `job.json` 存在 ≥5 秒、文件大小与记录一致
- `.srt` 曾因「先 truncate 再读」变 0 字节，已修
- Mac 端不做后台弹窗通知（LaunchAgent 读不了 CloudStorage），结果看「看结果」图标
- summary 用 `gemini` 就在命令里加 `-SummaryKey "你的 key"`；用 `none` 则不总结
