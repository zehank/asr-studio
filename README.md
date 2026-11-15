# ASR Studio

本地 / 跨机的「音频转文字 + 翻译 + 总结」工具。全部免费、模型可完全离线，中英混说也能识别。

```
音频/视频 ──► 转文字（whisper.cpp，本地 GPU）
          ├─► 翻译（Google 免费接口，中↔英自动判方向）
          └─► 总结（Ollama 本地模型 / Gemini / DeepSeek，任选）
```

## 三个用法，从简单到复杂

### 1. 命令行转写（最轻）

```bash
cd audio2text
./trans 录音.m4a                    # 出 录音.txt + 录音.srt
./trans 讲座.mp4 -l zh              # 指定语言
./trans 录音.m4a --terms "丙酮酸,线粒体"   # 专业词提示
```

输出：`.txt` 纯文本 + `.srt` 字幕。视频会先抽音轨。

### 2. 本地网页平台（拖文件就出结果）

```bash
cd studio && ./start      # 自动打开 http://127.0.0.1:8770
```

拖入音频/视频 → 勾选翻译、总结 → 一次跑完，结果分块展示，可复制可下载。
支持多个文件排队（同时最多 2 个），每个任务可单独取消。

### 3. 跨机流水线（重活在另一台机器上跑）

笔记本只负责投放，转写/翻译/总结在另一台机器（比如家里的 Windows + 独显）上做：

```
Mac 拖文件 ──► OneDrive（文件）──► Windows 接收端 ──► 本地模型处理
      └──────► Tailscale 直连信号（秒级触发）
                                          └──► 结果写回 OneDrive
```

- `studio/winlink/submit.py`：Mac 端投放（写 `job.json` + 发信号）
- `studio/winlink/receiver.py`：接收端（Mac/Windows 通用，Python 标准库）
- `studio/winlink/PROTOCOL.md`：两端契约（目录、字段、同步门槛）
- `studio/winlink/setup-win.ps1`：Windows 一键安装（whisper + 模型 + ffmpeg + 自启）

## 目录

| 路径 | 说明 |
|---|---|
| `audio2text/trans` | 命令行转写脚本（whisper.cpp） |
| `audio2text/README.md` | 命令行用法、原理、限制 |
| `studio/` | 本地网页平台（Python 标准库 HTTP 服务 + 单页前端） |
| `studio/winlink/` | 跨机流水线：投放器、接收端、协议、Windows 安装脚本 |

## 安装依赖

**whisper.cpp（转写引擎）**

```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
# 模型（先下 turbo，1.6G，速度约 4 倍实时）
mkdir -p models
curl -L -o models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

**平台本身**：只用系统自带的 Python 3（标准库），不需要 `pip install` 任何东西。

## 需要自己填的配置

`studio/winlink/config-mac.example.json` 复制成 `config-mac.json`，把 `summary.key` 换成你自己的：

- Gemini：<https://aistudio.google.com/apikey>（免费额度，不用绑卡）
- DeepSeek：<https://platform.deepseek.com>（便宜，一条讲座几分钱）
- 或者本地 Ollama：`ollama pull qwen3:14b`，把 `provider` 改成 `ollama`

**API key 不要提交到仓库**（`.gitignore` 已经排除了 `config*.json`）。

## 原理（一句话版）

whisper 把 16 kHz 音频切成 30 秒窗口 → 算 log-Mel 声谱图 → 编码器压成特征 → 解码器逐 token 自回归出字
→ 语言分类头决定中英 → 时间戳对回秒。`trans` 再删汉字间空格、半角标点换全角、过滤静音段幻觉。

细节见 `audio2text/README.md`。

## 许可证

MIT
