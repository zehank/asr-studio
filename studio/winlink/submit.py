#!/usr/bin/env python3
"""Mac 端投放器：把文件 + 任务写进 OneDrive 的 ASR/inbox，Windows 那头轮询处理。

用法:
  submit.py --ops transcribe,translate,summary 文件1 文件2 ...
"""
import argparse, json, os, shutil, subprocess, sys, time

DEFAULT_ROOT = os.path.expanduser("~/Library/CloudStorage/OneDrive-UniversityofWaterloo/ASR")
OPS = ("transcribe", "translate", "summary")
# 触发通道：文件走 OneDrive，信号走 SSH（Tailscale 内网地址）
SSH_HOST = os.environ.get("ASR_WIN_HOST", "rzhank@100.77.101.77")
SSH_TARGET = os.environ.get("ASR_WIN_CMD", "schtasks /run /tn ASRWorker")


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
    n = 2
    while os.path.exists(jobdir):          # 同一秒投同一个文件时避免撞名
        job_id = f"{stamp}-{n}-{base}"
        jobdir = os.path.join(inbox, job_id)
        n += 1
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

    # OneDrive 同步有延迟，第一次触发可能早于文件夹到位：90 秒后再补一枪
    import threading
    def _later():
        time.sleep(90)
        trigger_remote()
    threading.Thread(target=_later, daemon=True).start()

    # 立刻叫 Windows 干活：ssh 触发一个交互式计划任务
    # （OneDrive 的云占位文件在 SSH 的 session 0 里读不了，必须在用户会话里跑）
    ok, detail = trigger_remote()
    print(jobdir)
    print("trigger:", "ok" if ok else "failed — " + detail)
    return 0


def trigger_remote():
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "StrictHostKeyChecking=accept-new", SSH_HOST, SSH_TARGET]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False, "ssh 超时（Windows 关机或不在 Tailscale 上？）"
    except FileNotFoundError:
        return False, "本机没有 ssh 命令"
    if r.returncode == 0:
        return True, ""
    msg = (r.stderr or r.stdout or "").strip().splitlines()
    return False, (msg[0][:160] if msg else "ssh 返回 %d" % r.returncode)


if __name__ == "__main__":
    sys.exit(main())
