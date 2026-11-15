#!/usr/bin/env python3
# 本地一体平台：音频/视频 → 文字 → 翻译（Google）→ 总结（Pollinations 免费）
# 只用 Python 3.9 标准库，不需要装任何东西。
import json, os, re, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(ROOT)
ASR = os.path.join(WS, "audio2text")
BIN = os.path.join(ASR, "src", "build", "bin", "whisper-cli")
MODEL = os.path.join(ASR, "models", "ggml-large-v3-turbo.bin")
OUT = os.path.join(ROOT, "output")
PORT = int(os.environ.get("PORT", "8770"))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

RUNS = {}  # token -> {"event": threading.Event, "proc": Popen|None}
ASR_SEM = threading.Semaphore(2)  # 同时最多 2 个 whisper 进程，避免抢 GPU


class Cancelled(Exception):
    pass


def _check(token):
    r = RUNS.get(token) if token else None
    if r and r["event"].is_set():
        raise Cancelled()


def _sleep(secs, token=None):
    end = time.time() + secs
    while time.time() < end:
        _check(token)
        time.sleep(min(0.5, max(0.0, end - time.time())))


# ---------- 音频转文字 ----------
def to_wav(src, dst, tmp):
    ext = src.rsplit(".", 1)[-1].lower()
    if ext in ("wav", "mp3", "flac", "ogg"):
        shutil.copyfile(src, dst)
        return True
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst],
                       capture_output=True)
    if r.returncode == 0 and os.path.exists(dst):
        return True
    m4a = os.path.join(tmp, "a.m4a")
    r = subprocess.run(["avconvert", "--preset", "PresetAppleM4A", "--source", src, "--output", m4a],
                       capture_output=True)
    if r.returncode != 0 or not os.path.exists(m4a):
        return False
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", m4a, dst],
                       capture_output=True)
    return r.returncode == 0 and os.path.exists(dst)


BOILER_PATTERNS = [
    r"点赞", r"订阅", r"转发", r"打赏", r"明镜", r"点点栏目", r"字幕由", r"字幕提供",
    r"谢谢观看", r"感谢观看", r"请关注", r"小铃铛", r"subtitles? by", r"amara\.org",
]


def strip_boilerplate(text):
    """静音段常被模型硬编一句「请点赞订阅转发」，含 2 个以上关键词的行整行丢掉。"""
    keep = []
    for line in text.split("\n"):
        low = line.lower()
        hits = sum(1 for pat in BOILER_PATTERNS if re.search(pat, low))
        if hits >= 2:
            continue
        keep.append(line)
    return "\n".join(keep)


def tidy(text):
    cjk = r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]"
    text = re.sub(rf"(?<={cjk})[ \t]+(?={cjk})", "", text)
    for a, b in [(r",", "，"), (r"\.", "。"), (r"\?", "？"), (r"!", "！"), (r":", "："), (r";", "；")]:
        text = re.sub(rf"(?<={cjk}){a}", b, text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text)


def asr(path, outdir, base, lang, emit, token=None):
    tmp = tempfile.mkdtemp(prefix="asr.")
    wav = os.path.join(tmp, "in.wav")
    try:
        emit("音轨转换中…", 4)
        if not to_wav(path, wav, tmp):
            raise RuntimeError("这个文件的音轨读不出来（格式不支持？）")
        cmd = [BIN, "-m", MODEL, "-f", wav, "-l", lang, "-pp", "-t", str(os.cpu_count() or 4),
               "--output-txt", "--output-srt", "--output-file", os.path.join(outdir, base)]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
        if token in RUNS:
            RUNS[token]["proc"] = p
        last = -1
        try:
          for line in p.stderr:
            _check(token)
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                pct = int(m.group(1))
                if pct != last:
                    last = pct
                    emit(f"转写中… {pct}%", 5 + pct * 0.55)
        except Cancelled:
            try:
                p.kill()
            except Exception:
                pass
            raise
        if token in RUNS:
            RUNS[token]["proc"] = None
        if p.wait() != 0:
            _check(token)
            raise RuntimeError("whisper 识别失败")
        txt = os.path.join(outdir, base + ".txt")
        text = tidy(open(txt, encoding="utf-8").read()) if os.path.exists(txt) else ""
        text = strip_boilerplate(text)
        if os.path.exists(txt):
            open(txt, "w", encoding="utf-8").write(text)
        srt = os.path.join(outdir, base + ".srt")
        if os.path.exists(srt):
            srt_text = tidy(open(srt, encoding="utf-8").read())
            open(srt, "w", encoding="utf-8").write(srt_text)
        return text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 翻译（Google 免费接口） ----------
def is_chinese(text):
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk > max(8, len(text) * 0.05)


def _get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clients5(chunk, target):
    """Google 翻译的 dict-chrome 入口，比 gtx 更耐操。"""
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=%s&q=%s"
           % (target, urllib.parse.quote(chunk)))
    data = _get_json(url)
    if isinstance(data, list) and data:
        item = data[0]
        return item[0] if isinstance(item, list) else item
    raise RuntimeError("clients5 格式异常")


def _gtx(chunk, target):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=%s&dt=t&q=%s"
           % (target, urllib.parse.quote(chunk)))
    data = _get_json(url)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _mymemory(chunk, target):
    pair = "zh-CN|en" if target == "en" else "en|zh-CN"
    out = []
    for i in range(0, len(chunk), 450):
        piece = chunk[i:i + 450]
        url = "https://api.mymemory.translated.net/get?langpair=%s&q=%s" % (
            urllib.parse.quote(pair), urllib.parse.quote(piece))
        data = _get_json(url, timeout=30)
        out.append(data["responseData"]["translatedText"])
    return " ".join(out)


def translate_chunk(chunk, target):
    """先 Google（两个入口），都挂了退 MyMemory。都是免费、无需 key。"""
    last = None
    for attempt in range(2):
        for fn in (_clients5, _gtx):
            try:
                out = fn(chunk, target)
                if out and out.strip():
                    return out
            except Exception as e:
                last = e
        if attempt == 0:
            time.sleep(1.2)
    try:
        return _mymemory(chunk, target)
    except Exception:
        raise last or RuntimeError("翻译服务暂时不可用")


def translate(text, emit, target=None, token=None):
    if target in (None, "", "auto"):
        target = "en" if is_chinese(text) else "zh-CN"
    # 按行攒块，单行过长也硬切，避免 URL 过长被接口拒（400/414）
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > 900:
            piece, line = line[:900], line[900:]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(piece)
        if len(buf) + len(line) > 1000:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)

    out, failed = [], 0
    for i, chunk in enumerate(chunks):
        _check(token)
        try:
            out.append(translate_chunk(chunk, target))
        except Exception:
            failed += 1
            out.append("")
        emit(f"翻译中… {i + 1}/{len(chunks)}", 62 + (i + 1) / len(chunks) * 22)
    if failed == len(chunks):
        raise RuntimeError("翻译服务都不可用（Google 与 MyMemory 均失败）")
    return "\n".join(out), target


# ---------- 总结（可切换免费公共接口 / DeepSeek / Gemini / 本地 Ollama） ----------
CFG_PATH = os.path.join(ROOT, "config.json")
_LAST_LLM = [0.0]
API_PROVIDERS = ("deepseek", "gemini", "ollama")


def load_config():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CFG_PATH, 0o600)
    except Exception:
        pass


def _post_json(url, payload, headers=None, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        raise RuntimeError(f"接口返回 {e.code}：{detail or e.reason}")


def _llm_deepseek(messages, cfg):
    d = _post_json("https://api.deepseek.com/chat/completions",
                   {"model": cfg.get("model") or "deepseek-chat", "messages": messages, "stream": False},
                   {"Authorization": "Bearer " + cfg.get("key", "")})
    return d["choices"][0]["message"]["content"].strip()


GEMINI_PREFER = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash",
                 "gemini-2.5-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest",
                 "gemini-1.5-flash"]


def _gemini_list(key):
    """向 Google 问一下这把 key 到底能用哪些 flash 模型（有的 key 只有部分模型权限）。"""
    try:
        d = _get_json("https://generativelanguage.googleapis.com/v1beta/models?key=%s" % key, timeout=20)
    except Exception:
        return []
    out = []
    for m in d.get("models", []):
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        if not name.startswith("gemini") or "flash" not in name:
            continue
        if any(x in name for x in ("tts", "image", "audio", "live", "embedding")):
            continue
        if name.endswith("-latest"):
            continue  # 这类别名有时候会卡住很久，放最后再说
        out.append(name)
    return out


def _gemini_call(model, key, text, timeout):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (model, key))
    d = _post_json(url, {"contents": [{"parts": [{"text": text}]}],
                         "generationConfig": {"maxOutputTokens": 4096}}, timeout=timeout)
    return d["candidates"][0]["content"]["parts"][0]["text"].strip()


def _llm_gemini(messages, cfg):
    key = cfg.get("key", "")
    text = "\n\n".join(m["content"] for m in messages)

    # 先用小请求试通，选一个真能用的模型，记住它，下次不再试
    if cfg.get("model"):
        order = [cfg["model"]] + [m for m in (_gemini_list(key) or GEMINI_PREFER) if m != cfg["model"]]
    else:
        listed = _gemini_list(key)
        order = [m for m in GEMINI_PREFER if m in listed] + [m for m in listed if m not in GEMINI_PREFER]
        if not order:
            order = GEMINI_PREFER

    last = None
    for model in order[:6]:
        try:
            out = _gemini_call(model, key, text, timeout=180)
            if cfg.get("model") != model:
                cfg["model"] = model
                save_config(cfg)
            return out
        except Exception as e:
            last = e
            msg = str(e)
            if "404" in msg or "not found" in msg.lower() or "not supported" in msg.lower():
                continue  # 这个模型这把 key 用不了，换下一个
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                continue  # 卡住不动的直接跳过
            raise
    raise RuntimeError("Gemini 没找到能用的模型，最后一个错误：" + str(last)[:160])


def _llm_ollama(messages, cfg):
    d = _post_json("http://127.0.0.1:11434/api/chat",
                   {"model": cfg.get("model") or "qwen3:8b", "messages": messages, "stream": False},
                   timeout=900)
    return d["message"]["content"].strip()


def _llm_once(messages, timeout=240):
    body = json.dumps({"model": "openai", "messages": messages, "private": True}).encode()
    req = urllib.request.Request("https://text.pollinations.ai/openai", data=body,
                                 headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _llm_pollinations(messages, emit=None, timeout=240, token=None):
    """免费公共接口：每 IP 45 秒 1 次，主动排队 + 退避重试。"""
    last = None
    for attempt in range(4):
        _check(token)
        wait = 46 - (time.time() - _LAST_LLM[0])
        if wait > 0:
            if emit:
                emit(f"免费接口限流，等 {int(wait) + 1} 秒…", 88)
            _sleep(wait, token)
        _LAST_LLM[0] = time.time()
        try:
            return _llm_once(messages, timeout)
        except Exception as e:
            last = e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            if "FREE_LIMIT" in detail or "429" in str(e):
                _sleep(5, token)
                continue
            if attempt >= 2:
                break
            _sleep(3, token)
    raise last or RuntimeError("总结服务不可用")


def llm(messages, emit=None, timeout=240, token=None):
    cfg = load_config()
    prov = (cfg.get("provider") or "pollinations").lower()
    if prov == "deepseek":
        if not cfg.get("key"):
            raise RuntimeError("还没填 DeepSeek API key（页面右侧的 Key 框）")
        return _llm_deepseek(messages, cfg)
    if prov == "gemini":
        if not cfg.get("key"):
            raise RuntimeError("还没填 Gemini API key（页面右侧的 Key 框）")
        return _llm_gemini(messages, cfg)
    if prov == "ollama":
        return _llm_ollama(messages, cfg)
    return _llm_pollinations(messages, emit, timeout, token)


def summarize(text, emit, token=None):
    zh = is_chinese(text)
    lang_word = "中文" if zh else "English"
    lim = 30000 if (load_config().get("provider") or "pollinations").lower() in API_PROVIDERS else 6000
    chunks, buf = [], ""
    for p in [x for x in text.split("\n") if x.strip()]:
        if len(buf) + len(p) > lim:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    _check(token)
    emit(f"总结中… 共 {len(chunks)} 段", 86)
    if len(chunks) == 1:
        return llm([{"role": "user", "content":
                     f"下面是一段录音转写的文字。请用{lang_word}总结，分点列出：主要话题、关键结论、"
                     f"重要术语（保留原文用词）、行动项（如果有）。不要复述原文，不要编造。\n\n{chunks[0]}"}], emit, token=token)
    parts = []
    for i, c in enumerate(chunks):
        _check(token)
        parts.append(llm([{"role": "user", "content":
                           f"这是长录音的第 {i + 1}/{len(chunks)} 段。用{lang_word}提炼要点，只写条目，"
                           f"保留数字、术语和定义。\n\n{c}"}], emit, token=token))
        emit(f"总结中… {i + 1}/{len(chunks)}", 86 + (i + 1) / len(chunks) * 8)
    return llm([{"role": "user", "content":
                 f"下面是对一段长录音分段提炼的要点。请合并成一份{lang_word}总结，按主题分点，"
                 f"去掉重复，保留所有术语和数字。\n\n" + "\n\n".join(parts)}], emit, token=token)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(ROOT, "index.html"), "text/html; charset=utf-8")
        if path == "/api/config":
            cfg = load_config()
            return self.send_json({"provider": cfg.get("provider") or "pollinations",
                                   "model": cfg.get("model") or "",
                                   "hasKey": bool(cfg.get("key")),
                                   "keyHint": ("…" + cfg["key"][-4:]) if cfg.get("key") else ""})
        if path.startswith("/out/"):
            rel = urllib.parse.unquote(path[5:])
            full = os.path.realpath(os.path.join(OUT, rel))
            if not full.startswith(os.path.realpath(OUT)) or not os.path.isfile(full):
                return self.send_json({"error": "not found"}, 404)
            ctype = "text/plain; charset=utf-8" if full.endswith((".txt", ".md", ".srt")) else "application/octet-stream"
            return self.send_file(full, ctype)
        return self.send_json({"ok": True, "app": "studio"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/config":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return self.send_json({"ok": False, "error": "请求格式不对"}, 400)
            cfg = load_config()
            cfg["provider"] = (body.get("provider") or "pollinations").lower()
            cfg["model"] = (body.get("model") or "").strip()
            key = (body.get("key") or "").strip()
            if key:
                cfg["key"] = key
            if cfg["provider"] == "pollinations":
                cfg.pop("key", None)
            save_config(cfg)
            return self.send_json({"ok": True, "provider": cfg["provider"],
                                   "hasKey": bool(cfg.get("key"))})

        if urllib.parse.urlparse(self.path).path == "/api/cancel":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = q.get("token", [""])[0]
            r = RUNS.get(token)
            if not r:
                return self.send_json({"ok": False, "error": "没有这个任务"}, 404)
            r["event"].set()
            p = r.get("proc")
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
            return self.send_json({"ok": True})

        if urllib.parse.urlparse(self.path).path == "/api/open":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            job = q.get("job", [""])[0]
            full = os.path.realpath(os.path.join(OUT, job))
            if job and full.startswith(os.path.realpath(OUT)) and os.path.isdir(full):
                subprocess.run(["open", full])
                return self.send_json({"ok": True, "path": full})
            return self.send_json({"ok": False, "error": "文件夹不存在"}, 404)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = urllib.parse.unquote(self.headers.get("X-Filename", "audio.m4a"))
        name = re.sub(r"[^\w.\-\u4e00-\u9fff ]", "_", os.path.basename(name))[:80] or "audio.m4a"
        do_tr = q.get("translate", ["0"])[0] == "1"
        do_sum = q.get("summary", ["0"])[0] == "1"
        lang = q.get("lang", ["auto"])[0]
        base = os.path.splitext(name)[0][:60] or "clip"
        job = time.strftime("%m%d-%H%M%S") + "-" + base
        outdir = os.path.join(OUT, job)
        os.makedirs(outdir, exist_ok=True)
        src = os.path.join(outdir, "source-" + name)
        token = "%s-%d" % (job, int(time.time() * 1000) % 100000)
        RUNS[token] = {"event": threading.Event(), "proc": None}

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(msg, pct):
            try:
                self.wfile.write((json.dumps({"type": "progress", "msg": msg, "pct": round(pct, 1)},
                                             ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except Exception:
                # 页面被关掉 / 连接断了 → 当作取消，别让任务空转
                RUNS[token]["event"].set()
                raise Cancelled()

        try:
            self.wfile.write((json.dumps({"type": "start", "token": token, "job": job},
                                         ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
            left = int(self.headers.get("Content-Length", 0))
            emit("接收文件…", 1)
            with open(src, "wb") as f:
                while left > 0:
                    buf = self.rfile.read(min(left, 1 << 20))
                    if not buf:
                        break
                    f.write(buf)
                    left -= len(buf)

            if ASR_SEM.acquire(blocking=False):
                try:
                    text = asr(src, outdir, base, lang, emit, token)
                finally:
                    ASR_SEM.release()
            else:
                emit("排队等 GPU…", 3)
                ASR_SEM.acquire()
                try:
                    text = asr(src, outdir, base, lang, emit, token)
                finally:
                    ASR_SEM.release()
            emit("转写完成", 60)
            open(os.path.join(outdir, "text.txt"), "w", encoding="utf-8").write(text)

            result = {"type": "done", "job": job, "text": text, "files": {
                "text": f"/out/{urllib.parse.quote(job)}/{urllib.parse.quote(base)}.txt",
                "srt": f"/out/{urllib.parse.quote(job)}/{urllib.parse.quote(base)}.srt"}}

            warn = []
            if do_tr and text.strip():
                try:
                    tr, target = translate(text, emit, q.get("dir", ["auto"])[0], token)
                    open(os.path.join(outdir, "translation.txt"), "w", encoding="utf-8").write(tr)
                    result["translation"] = tr
                    result["target"] = target
                except Exception as e:
                    warn.append("翻译失败：" + str(e)[:120])

            if do_sum and text.strip():
                try:
                    sm = summarize(text, emit, token)
                    open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8").write(sm)
                    result["summary"] = sm
                except Exception as e:
                    warn.append("总结失败：" + str(e)[:120])

            if warn:
                result["warning"] = "；".join(warn)

            emit("完成", 100)
            result["dir"] = job
            self.wfile.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        except Cancelled:
            traceback_note = "任务已取消"
            try:
                self.wfile.write((json.dumps({"type": "cancelled", "error": traceback_note},
                                             ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except Exception:
                pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.wfile.write((json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except Exception:
                pass
        finally:
            RUNS.pop(token, None)

    # helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(BIN):
        print("找不到 whisper-cli:", BIN, file=sys.stderr)
        sys.exit(1)
    port = PORT
    for _ in range(12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("没有可用端口", file=sys.stderr)
        sys.exit(1)
    print(f"studio 已启动 → http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
