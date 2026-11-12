"""子进程执行、输出捕获与实时日志。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from .errors import TransportError


class ProcessTimeout(TransportError):
    pass


class RunResult:
    def __init__(self, rc, stdout, stderr, duration, timed_out=False):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.timed_out = timed_out

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out

    def summary(self, max_chars=1000):
        text = (self.stderr or "").strip()
        if not text:
            text = (self.stdout or "").strip()
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text


def _kill_process(proc):
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_process(argv, input_data=None, timeout=900, env=None, cwd=None,
                stream_stderr=None, stream_stdout=None, prefix="", log=None):
    """运行命令。

    argv 为列表（本地）或 shell=True 时可为字符串。
    stream_stderr/stream_stdout: callable(line) 用于实时输出。
    返回 RunResult；超时抛 ProcessTimeout。
    """
    if env is None:
        env = os.environ.copy()
    if input_data is not None and isinstance(input_data, str):
        input_data = input_data.encode("utf-8")

    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": cwd,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise TransportError("命令不存在：%s" % (argv[0] if isinstance(argv, list) else argv))
    except OSError as exc:
        raise TransportError("无法启动命令：%s" % exc)

    stdout_buf = []
    stderr_buf = []
    lock = threading.Lock()

    def reader(pipe, buf, callback):
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            with lock:
                buf.append(chunk)
            if callback:
                try:
                    text = chunk.decode("utf-8", "replace")
                    for line in text.splitlines():
                        if line.strip():
                            callback(line)
                except Exception:
                    pass
        try:
            pipe.close()
        except Exception:
            pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_buf, stream_stdout), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_buf, stream_stderr), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    start = time.time()
    try:
        if input_data is not None:
            try:
                proc.stdin.write(input_data)
            except (BrokenPipeError, OSError):
                pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    finally:
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        try:
            if proc.poll() is None:
                _kill_process(proc)
        except Exception:
            pass

    stdout = b"".join(stdout_buf).decode("utf-8", "replace")
    stderr = b"".join(stderr_buf).decode("utf-8", "replace")
    duration = time.time() - start
    if timed_out:
        raise ProcessTimeout("命令执行超时（%ss）%s" % (timeout, ("：" + prefix) if prefix else ""))
    return RunResult(proc.returncode, stdout, stderr, duration)


def run_shell(command, input_data=None, timeout=900, env=None, cwd=None,
              stream_stderr=None, stream_stdout=None, prefix=""):
    """在 POSIX shell 中执行字符串命令。"""
    import shlex
    shell = "/bin/sh"
    return run_process([shell, "-c", command], input_data=input_data, timeout=timeout,
                       env=env, cwd=cwd, stream_stderr=stream_stderr,
                       stream_stdout=stream_stdout, prefix=prefix)
