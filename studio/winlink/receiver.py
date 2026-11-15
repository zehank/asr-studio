#!/usr/bin/env python3
"""接收端：盯着 OneDrive 的 ASR/inbox，做转写 / 翻译 / 总结，结果写回 ASR/out。

Mac 和 Windows 跑同一份代码，差别只在 config.json 里的路径。
只依赖 Python 标准库 + 外部的 whisper-cli / ffmpeg 可执行文件。

    python3 receiver.py --config config.json        # 一直跑
    python3 receiver.py --config config.json --once  # 只扫一遍就退出
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.parse
import urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    cmd = [cfg["whisper_bin"], "-m", cfg["model"], "-f", wav, "-l", lang or "auto",
           "--output-txt", "--output-srt", "--output-file", outbase]
    if cfg.get("threads"):
        cmd += ["-t", str(cfg["threads"])]
    log("  whisper 开始…")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        raise RuntimeError("whisper 失败：" + (r.stderr or "")[-300:])
    txt = outbase + ".txt"
    text = ""
    if os.path.exists(txt):
        text = strip_boilerplate(tidy(open(txt, encoding="utf-8").read()))
        open(txt, "w", encoding="utf-8").write(text)
    srt = outbase + ".srt"
    if os.path.exists(srt):
        srt_text = tidy(open(srt, encoding="utf-8").read())
        open(srt, "w", encoding="utf-8").write(srt_text)
    return text


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
        body = {"model": s.get("model") or "qwen3:14b", "stream": False,
                "messages": [{"role": "user", "content": prompt}]}
        return _post(s.get("url") or "http://127.0.0.1:11434/api/chat", body, {}, 1800)[
            "message"]["content"].strip()
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
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("接口 %s：%s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))


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
    done_files, err = [], None
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
                sm = summarize(text, cfg, log)
                if sm:
                    open(os.path.join(outdir, base + ".summary.md"), "w", encoding="utf-8").write(sm)
                    item["summary"] = base + ".summary.md"
            done_files.append(item)
        state = "done"
    except Exception as e:
        state, err = "failed", str(e)[:500]
        log(" 失败：" + err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    status = {"id": jid, "state": state, "files": done_files,
              "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}
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
            continue
        try:
            job = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        if job.get("state") not in ("queued", "failed"):
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


def serve_signal(cfg_get, wake, port, token, log):
    """一个小 HTTP 服务：Mac 投完任务就 ding 一下，立刻扫描，不靠轮询时延。"""

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            b = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._reply(200, {"ok": True, "service": "asr-signal"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return self._reply(400, {"ok": False, "error": "bad json"})
            if token and body.get("token") != token:
                return self._reply(403, {"ok": False, "error": "bad token"})
            log("收到信号：" + str(body.get("id", "")))
            wake.set()
            self._reply(200, {"ok": True})

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    log(f"信号服务监听 0.0.0.0:{port}")
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)   # 没人发信号时的兜底轮询
    ap.add_argument("--listen", type=int, default=0, help="信号服务端口，0 表示不开")
    a = ap.parse_args()

    def log(msg):
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

    wake = threading.Event()

    # 信号服务：端口从 --listen 或配置里取
    port, token = a.listen, ""
    try:
        c0 = json.load(open(a.config, encoding="utf-8-sig"))
        sig = c0.get("signal") or {}
        port = port or int(sig.get("port") or 0)
        token = sig.get("token") or ""
    except Exception:
        pass
    if port:
        threading.Thread(target=serve_signal, args=(None, wake, port, token, log), daemon=True).start()

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
            return 0
        wait = 4 if left else a.interval
        wake.wait(timeout=wait)
        wake.clear()


if __name__ == "__main__":
    sys.exit(main())
