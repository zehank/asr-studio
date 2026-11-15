# 两端契约（Mac 投放端 ↔ Windows 接收端）

Mac 端已经实现并固定，Windows 端只要照这个契约实现就能直接跑通。
**改契约要两边一起改**，否则 Mac 投的任务对不上。

## 1. 传输

| | 用什么 | 说明 |
|---|---|---|
| 文件 | Waterloo OneDrive | 根目录 `OneDrive - University of Waterloo\ASR`；大文件靠它同步、断点续传 |
| 触发 | SSH over Tailscale | 投放端执行一条命令，让对端跑一次 `receiver.py --once` |

触发命令（投放端发起，`rzhank@100.77.101.77` 是接收端）：

```
ssh -o BatchMode=yes -o ConnectTimeout=5 rzhank@100.77.101.77 \
  cmd /c start "asr" /min C:\ASR\python\python.exe C:\ASR\receiver.py --config C:\ASR\config.json --once
```

`start /min` 让接收端脱离 SSH 会话继续跑，所以投放端合盖/断线不影响它做完。

接收端**不需要常驻**，也不监听任何端口：被叫一次、扫一遍 inbox、做完退出。

## 2. 目录

```
ASR/
  inbox/<task-id>/          投放方写
      <原文件名>.m4a|.mp3|.mp4|...
      job.json
  out/<task-id>/            接收端写
      <名字>.txt
      <名字>.srt
      <名字>.translation.txt   （选了翻译才有）
      <名字>.summary.md        （选了总结才有）
      status.json
  tools/                    安装脚本与文档
```

## 3. job.json（投放方写，接收端只改 `state`）

```json
{
  "id": "20261115-155923-source-标准录音 7",
  "files": [{"name": "source-标准录音 7.mp3", "size": 102045865}],
  "ops": ["transcribe", "translate"],
  "lang": "auto",
  "source": "mac",
  "created": "2026-11-15T15:59:23",
  "state": "queued"
}
```

- `ops` 取值：`transcribe`（必做）、`translate`（中→英 / 英→中自动判方向）、`summary`
- `state`：`queued` → 处理中写 `processing`（可选）→ 结束写 `done` 或 `failed`
- **接收端必须先把 `state` 从 `queued` 改成别的，避免重启后重复处理**

## 4. status.json（接收端写）

```json
{
  "id": "20261115-155923-source-标准录音 7",
  "state": "done",
  "files": [{"name": "source-标准录音 7.mp3", "text": "source-标准录音 7.txt",
             "srt": "source-标准录音 7.srt", "translation": "source-标准录音 7.translation.txt"}],
  "finished": "2026-11-15T16:40:00",
  "error": "只在 failed 时出现"
}
```

## 5. 同步门槛（重要）

OneDrive 上文件是「按需下载」，刚开始可能只同步了一半：

- `job.json` 的修改时间要 **≥5 秒** 前
- 每个 `files[].size` 要和磁盘上实际大小**一致**
- 不满足就跳过，下一轮再看（不要把半个文件喂给 whisper）

## 6. 处理细节

- 音视频先转 16 kHz 单声道 wav（ffmpeg / macOS 上 afconvert）
- 转写：`whisper-cli -m <模型> -f <wav> -l <lang> --output-txt --output-srt --output-file <out前缀>`
- 文本后处理：汉字之间的空格删掉；中文后面的半角标点换全角；含「点赞/订阅/转发/打赏/明镜/点点栏目/字幕由/谢谢观看」≥2 个关键词的行整行丢弃（静音段幻觉）
- 翻译：Google 免费接口（`clients5.google.com/translate_a/t`，失败退 `api.mymemory.translated.net`）
- 总结：Ollama（`/api/chat`）/ Gemini / DeepSeek，任选；用哪个写在 `config.json` 的 `summary.provider`

## 7. 参考实现

`receiver.py` 就是按这个契约实现的完整版本（Python 标准库 + 外部 whisper-cli/ffmpeg），
Windows 端可以直接用，也可以照它重写。
