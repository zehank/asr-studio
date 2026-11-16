#!/usr/bin/env python3
"""接收端：盯着 OneDrive 的 ASR/inbox，做转写 / 翻译 / 总结，结果写回 ASR/out。

Mac 和 Windows 跑同一份代码，差别只在 config.json 里的路径。
只依赖 Python 标准库 + 外部的 whisper-cli / ffmpeg 可执行文件。

    python3 receiver.py --config config.json        # 一直跑
    python3 receiver.py --config config.json --once  # 只扫一遍就退出
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse
import urllib.error, urllib.request

# Windows 控制台默认 cp1252，遇到中文文件名 print 会直接崩；先统一成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CJK = r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]"
BOILER = [r"点赞", r"订阅", r"转发", r"打赏", r"明镜", r"点点栏目", r"字幕由",
          r"谢谢观看", r"感谢观看", r"请关注", r"小铃铛", r"subtitles? by", r"amara\.org"]
UA = {"User-Agent": "Mozilla/5.0"}


# ---------- 文本清洗 ----------
def strip_boilerplate(text):
    out = []
    for line in text.split("\n"):
        low = line.lower()
        if sum(1 for p in BOILER if re.search(p, low)) >= 2:
            continue
        out.append(line)
    return "\n".join(out)


def tidy(text):
    text = re.sub(rf"(?<={CJK})[ \t]+(?={CJK})", "", text)
    for a, b in [(",", "，"), (".", "。"), ("?", "？"), ("!", "！"), (":", "："), (";", "；")]:
        text = re.sub(rf"(?<={CJK}){re.escape(a)}", b, text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text)


# ---------- 音视频 → wav ----------
def to_wav(src, dst, cfg):
    ff = cfg.get("ffmpeg")
    if ff and os.path.exists(ff):
        r = subprocess.run([ff, "-y", "-i", src, "-ar", "16000", "-ac", "1", "-vn", dst],
                           capture_output=True)
        return r.returncode == 0 and os.path.exists(dst)
    # 没配 ffmpeg 时的 macOS 兜底
    if sys.platform == "darwin":
        if subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst],
                          capture_output=True).returncode == 0:
            return os.path.exists(dst)
        m4a = dst + ".m4a"
        subprocess.run(["avconvert", "--preset", "PresetAppleM4A", "--source", src, "--output", m4a],
                       capture_output=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", m4a, dst],
                       capture_output=True)
        return os.path.exists(dst)
    return False


# ---------- 转写 ----------
def transcribe(wav, outbase, lang, cfg, log):
    """Windows 上 whisper-cli 用窄字符 argv，中文路径会变成 ????；
    所以一律在纯 ASCII 的临时目录里跑，再把结果搬到目标目录。"""
    tmp = tempfile.mkdtemp(prefix="asr.")
    try:
        ascii_wav = os.path.join(tmp, "in.wav")
        shutil.copyfile(wav, ascii_wav)
        ascii_out = os.path.join(tmp, "out")
        cmd = [cfg["whisper_bin"], "-m", cfg["model"], "-f", ascii_wav, "-l", lang or "auto",
               "--output-txt", "--output-srt", "--output-file", ascii_out]
        if cfg.get("threads"):
            cmd += ["-t", str(cfg["threads"])]
        log("  whisper 开始…")
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if r.returncode != 0:
            raise RuntimeError("whisper 失败：" + (r.stderr or "")[:400])

        text = ""
        src_txt = ascii_out + ".txt"
        if os.path.exists(src_txt):
            text = strip_boilerplate(tidy(open(src_txt, encoding="utf-8").read()))
            open(outbase + ".txt", "w", encoding="utf-8").write(text)
        src_srt = ascii_out + ".srt"
        if os.path.exists(src_srt):
            open(outbase + ".srt", "w", encoding="utf-8").write(
                tidy(open(src_srt, encoding="utf-8").read()))
        return text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 翻译 ----------
def is_chinese(t):
    return len(re.findall(r"[\u4e00-\u9fff]", t)) > max(8, len(t) * 0.05)


def tr_chunk(chunk, target):
    for fn in (_google, _mymemory):
        try:
            out = fn(chunk, target)
            if out and out.strip():
                return out
        except Exception:
            pass
    return ""


def _google(chunk, target):
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=%s&q=%s"
           % (target, urllib.parse.quote(chunk)))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    item = d[0]
    return item[0] if isinstance(item, list) else item


def _mymemory(chunk, target):
    pair = "zh-CN|en" if target == "en" else "en|zh-CN"
    out = []
    for i in range(0, len(chunk), 450):
        url = ("https://api.mymemory.translated.net/get?langpair=%s&q=%s"
               % (urllib.parse.quote(pair), urllib.parse.quote(chunk[i:i + 450])))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            out.append(json.loads(r.read().decode("utf-8"))["responseData"]["translatedText"])
    return " ".join(out)


def translate(text, log):
    target = "en" if is_chinese(text) else "zh-CN"
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > 900:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:900])
            line = line[900:]
        if len(buf) + len(line) > 1000:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    res = []
    for i, c in enumerate(chunks):
        res.append(tr_chunk(c, target))
        log(f"  翻译 {i + 1}/{len(chunks)}")
    return "\n".join(res)


# ---------- 总结 ----------
def _ollama_up(url):
    try:
        urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3).read()
        return True
    except Exception:
        return False


def ensure_ollama(url, log):
    if _ollama_up(url):
        return True
    exe = shutil.which("ollama") or os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    if not exe or not os.path.exists(exe):
        return False
    log(" Ollama 没在跑，拉起来…")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe, "serve"], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(" 拉不起来：" + str(e)[:80])
        return False
    for _ in range(20):
        time.sleep(3)
        if _ollama_up(url):
            log(" Ollama 已就绪")
            return True
    return False


def summarize(text, cfg, log):
    s = cfg.get("summary", {})
    prov = (s.get("provider") or "none").lower()
    if prov == "none":
        return ""
    lang = "中文" if is_chinese(text) else "English"
    prompt = (f"下面是一段录音转写的文字。请用{lang}总结，分点列出：主要话题、关键结论、"
              f"重要术语（保留原文用词）、行动项（如果有）。不要复述原文，不要编造。\n\n{text}")
    log(f"  总结（{prov}）…")
    if prov == "ollama":
        ensure_ollama(s.get("url") or "http://127.0.0.1:11434", log)
        opts = {}
        if s.get("num_ctx"):
            opts["num_ctx"] = int(s["num_ctx"])
        body = {"model": s.get("model") or "qwen3:14b", "stream": False,
                "messages": [{"role": "user", "content": prompt}]}
        if opts:
            body["options"] = opts
        if "think" in s:
            body["think"] = bool(s["think"])
        base = (s.get("url") or "http://127.0.0.1:11434").rstrip("/")
        if not base.endswith("/api/chat"):
            base += "/api/chat"
        return _post(base, body, {}, 1800)["message"]["content"].strip()
    if prov == "gemini":
        url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
               % (s.get("model") or "gemini-2.5-flash", s.get("key", "")))
        d = _post(url, {"contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": 4096}}, {}, 300)
        return d["candidates"][0]["content"]["parts"][0]["text"].strip()
    if prov == "deepseek":
        body = {"model": s.get("model") or "deepseek-chat", "stream": False,
                "messages": [{"role": "user", "content": prompt}]}
        return _post("https://api.deepseek.com/chat/completions", body,
                     {"Authorization": "Bearer " + s.get("key", "")}, 300)["choices"][0]["message"][
                         "content"].strip()
    raise RuntimeError("不认识的总结提供方：" + prov)


def _post(url, payload, headers, timeout):
    """带一次重试：Ollama 刚被拉起来时偶尔会立刻断开（WinError 10054）。"""
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError("接口 %s：%s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(4)
    raise RuntimeError("连接失败：" + str(last))


# ---------- 单个任务 ----------
def process_job(jobdir, cfg, log):
    job = json.load(open(os.path.join(jobdir, "job.json"), encoding="utf-8"))
    jid = job["id"]
    ops = job.get("ops") or ["transcribe"]
    outdir = os.path.join(cfg["root"], "out", jid)
    os.makedirs(outdir, exist_ok=True)

    status = {"id": jid, "state": "processing", "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    json.dump(status, open(os.path.join(outdir, "status.json"), "w", encoding="utf-8"), ensure_ascii=False)

    tmp = tempfile.mkdtemp(prefix="rx.")
    done_files, err, warn = [], None, []
    try:
        for f in job.get("files", []):
            src = os.path.join(jobdir, f["name"])
            base = os.path.splitext(f["name"])[0]
            log(f" 处理 {f['name']}")
            wav = os.path.join(tmp, base + ".wav")
            if not to_wav(src, wav, cfg):
                raise RuntimeError("音轨读不出来：" + f["name"])
            text = transcribe(wav, os.path.join(outdir, base), job.get("lang", "auto"), cfg, log)
            item = {"name": f["name"], "text": base + ".txt", "srt": base + ".srt"}
            if "translate" in ops and text.strip():
                tr = translate(text, log)
                open(os.path.join(outdir, base + ".translation.txt"), "w", encoding="utf-8").write(tr)
                item["translation"] = base + ".translation.txt"
            if "summary" in ops and text.strip():
                try:
                    sm = summarize(text, cfg, log)
                    if sm:
                        open(os.path.join(outdir, base + ".summary.md"), "w", encoding="utf-8").write(sm)
                        item["summary"] = base + ".summary.md"
                except Exception as e:
                    warn.append("总结失败：" + str(e)[:120])
                    log(" 总结失败，其余照常：" + str(e)[:80])
            done_files.append(item)
        state = "done"
    except Exception as e:
        state, err = "failed", str(e)[:500]
        log(" 失败：" + err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    status = {"id": jid, "state": state, "files": done_files,
              "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if warn:
        status["warning"] = "；".join(warn)
    if err:
        status["error"] = err
    json.dump(status, open(os.path.join(outdir, "status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    job["state"] = state
    json.dump(job, open(os.path.join(jobdir, "job.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return state


def scan(cfg, log):
    inbox = os.path.join(cfg["root"], "inbox")
    if not os.path.isdir(inbox):
        return 0
    n = 0
    left = 0
    for name in sorted(os.listdir(inbox)):
        jobdir = os.path.join(inbox, name)
        jf = os.path.join(jobdir, "job.json")
        if not os.path.isfile(jf):
            # 文件夹已同步、job.json 还没下来：算作待办，别让这次触发白跑
            try:
                if time.time() - os.path.getmtime(jobdir) < 600:
                    left += 1
            except OSError:
                pass
            continue
        try:
            job = json.load(open(jf, encoding="utf-8-sig"))
        except Exception:
            # 常见于 OneDrive 还没把这个文件同步到本地（云占位文件读不了）
            if time.time() - os.path.getmtime(jf) < 600:
                left += 1
            continue
        if job.get("state") != "queued":
            continue
        left += 1
        # 等 OneDrive 同步落盘：job 至少要 5 秒前，且文件大小跟 job.json 里记的一致
        if time.time() - os.path.getmtime(jf) < 5:
            continue
        pending = []
        for f in job.get("files", []):
            fp = os.path.join(jobdir, f["name"])
            if not os.path.exists(fp) or os.path.getsize(fp) != f.get("size", -1):
                pending.append(f["name"])
        if pending:
            log(f" 等同步：{name}（{', '.join(pending)}）")
            continue
        log(f"▶ {name}")
        process_job(jobdir, cfg, log)
        n += 1
        left -= 1
    return left


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)   # 常驻模式下的兜底轮询间隔
    a = ap.parse_args()

    def log(msg):
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

    while True:
        try:
            cfg = json.load(open(a.config, encoding="utf-8-sig"))
        except Exception as e:
            log("读不到配置 %s：%s" % (a.config, e))
            if a.once:
                return 2
            time.sleep(10)
            continue
        left = scan(cfg, log)
        if a.once:
            # 被 SSH 叫一次就守到最后：OneDrive 还没同步完的会一直等
            deadline = time.time() + 2700
            while left > 0 and time.time() < deadline:
                time.sleep(5)
                left = scan(cfg, log)
            return 0
        wait = 4 if left else a.interval
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
