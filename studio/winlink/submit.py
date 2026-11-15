#!/usr/bin/env python3
"""Mac 端投放器：把文件 + 任务写进 OneDrive 的 ASR/inbox，Windows 那头轮询处理。

用法:
  submit.py --ops transcribe,translate,summary 文件1 文件2 ...
"""
import argparse, json, os, shutil, sys, time, urllib.request

DEFAULT_ROOT = os.path.expanduser("~/Library/CloudStorage/OneDrive-UniversityofWaterloo/ASR")
OPS = ("transcribe", "translate", "summary")
# 信号通道：Tailscale 直连 Windows（文件仍走 OneDrive）
SIGNAL_URL = os.environ.get("ASR_SIGNAL_URL", "http://100.77.101.77:8765/job")
SIGNAL_TOKEN = os.environ.get("ASR_SIGNAL_TOKEN", "asr-9c41f7b2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", default="transcribe")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--lang", default="auto")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()

    ops = [o.strip() for o in a.ops.split(",") if o.strip() in OPS]
    if not ops:
        print("没有有效任务", file=sys.stderr)
        return 2

    inbox = os.path.join(a.root, "inbox")
    os.makedirs(inbox, exist_ok=True)

    files = [f for f in a.files if os.path.isfile(f)]
    if not files:
        print("没有可投放的文件", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(os.path.basename(files[0]))[0][:40]
    job_id = f"{stamp}-{base}"
    jobdir = os.path.join(inbox, job_id)
    os.makedirs(jobdir, exist_ok=True)

    info = []
    for f in files:
        dst = os.path.join(jobdir, os.path.basename(f))
        if os.path.abspath(dst) != os.path.abspath(f):
            shutil.copy2(f, dst)
        info.append({"name": os.path.basename(f), "size": os.path.getsize(dst)})

    job = {"id": job_id, "files": info, "ops": ops, "lang": a.lang,
           "source": "mac", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "state": "queued"}
    with open(os.path.join(jobdir, "job.json"), "w", encoding="utf-8") as fp:
        json.dump(job, fp, ensure_ascii=False, indent=2)

    # 立刻给 Windows 发一个信号；失败也不要紧，那边还有兜底轮询
    signal_ok = ping_signal(job_id)
    print(jobdir)
    print("signal:", "sent" if signal_ok else "skipped/unreachable")
    return 0


def ping_signal(job_id):
    try:
        body = json.dumps({"id": job_id, "token": SIGNAL_TOKEN}).encode()
        req = urllib.request.Request(SIGNAL_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
