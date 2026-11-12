"""agent 基础工具：root 锁、命令执行、文件写入、下载。"""

from __future__ import annotations

import errno
try:
    import fcntl
except ImportError:  # Windows 仅用于本地单元测试；agent 只部署在 Linux
    fcntl = None
import hashlib
import os
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .constants import AGENT_STATE_DIR
from .util import atomic_write, ensure_dir, tail


LOG_PREFIX = "[kubedeploy]"
_LOCK_PATH = "/var/lock/kube-deploy.lock"
_CA_CANDIDATES = (
    "/etc/pki/tls/certs/ca-bundle.crt",                 # RHEL/CentOS 8+
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", # RHEL/CentOS
    "/etc/ssl/certs/ca-certificates.crt",               # Debian/Ubuntu
    "/etc/ssl/cert.pem",                                # Alpine/部分发行版
    "/etc/ssl/certs",                                   # 目录形式
)
_SSL_CONTEXT = None
_SECRET_PATTERNS = [
    re.compile(r"(?i)(--token(?:=|\s+))[^\s]+"),
    re.compile(r"(?i)(--certificate-key(?:=|\s+))[^\s]+"),
    re.compile(r"(?i)(--discovery-token-ca-cert-hash(?:=|\s+))[^\s]+"),
    re.compile(r"(?i)(token\s+)[A-Za-z0-9._-]{6,}"),
    re.compile(r"(?i)(certificate-key\s+)[A-Za-z0-9._-]{6,}"),
]


class ActionFailure(Exception):
    def __init__(self, message, errors=None, hint=None):
        super().__init__(message)
        self.message = message
        self.errors = list(errors or [message])
        self.hint = hint


def log(message):
    sys.stderr.write("%s %s\n" % (LOG_PREFIX, message))
    sys.stderr.flush()


def debug(message):
    if os.environ.get("KUBEDEPLOY_DEBUG"):
        log("[debug] " + str(message))


def redact_command(command):
    text = command if isinstance(command, str) else " ".join(str(x) for x in command)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


def redact_text(text):
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


class Lock:
    def __init__(self, path=_LOCK_PATH):
        self.path = path
        self.fh = None

    def __enter__(self):
        ensure_dir(os.path.dirname(self.path), 0o755)
        self.fh = open(self.path, "a+")
        if fcntl is None:
            return self
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ActionFailure("同一节点上的 KubeDeploy 操作正在运行，请稍后重试")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write("pid=%d time=%s\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
        self.fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
        except Exception:
            pass
        return False


def run(command, timeout=900, check=True, env=None, input_data=None, cwd=None):
    if isinstance(command, (list, tuple)):
        display = " ".join(str(x) for x in command)
        shell = False
    else:
        display = command
        shell = True
    log("$ " + redact_command(display))
    kwargs = {
        "shell": shell,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "cwd": cwd,
    }
    if env is not None:
        kwargs["env"] = env
    if input_data is not None:
        if isinstance(input_data, str):
            input_data = input_data.encode("utf-8")
        kwargs["input"] = input_data
    try:
        proc = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        raise ActionFailure("命令超时（%ss）：%s" % (timeout, redact_command(display)))
    except FileNotFoundError:
        raise ActionFailure("命令不存在：%s" % (command if isinstance(command, str) else command[0]))
    except OSError as exc:
        raise ActionFailure("命令执行失败：%s" % exc)
    stdout = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
    stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
    if stdout.strip():
        for line in redact_text(stdout).rstrip().splitlines()[-30:]:
            log("  " + line)
    if proc.returncode != 0:
        if stderr.strip():
            for line in redact_text(stderr).rstrip().splitlines()[-30:]:
                log("  ! " + line)
        if check:
            raise ActionFailure("命令失败（rc=%d）：%s" % (proc.returncode, redact_command(display)),
                                errors=[redact_text(tail(stderr, 15)) or "命令返回非零状态"])
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def run_ok(command, timeout=300, **kwargs):
    try:
        return run(command, timeout=timeout, check=False, **kwargs)
    except ActionFailure as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def which(name):
    return shutil.which(name)


def read_file(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def write_file(path, content, mode=0o644):
    atomic_write(path, content, mode)


def backup_file(path, suffix=None):
    if not os.path.exists(path):
        return None
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    dest = "%s.kubedeploy-%s.bak" % (path, suffix)
    shutil.copy2(path, dest)
    return dest


def remove_file(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)
    except OSError as exc:
        log("清理 %s 失败：%s" % (path, exc))


def ensure_root():
    if os.geteuid() != 0:
        raise ActionFailure("agent 需要 root 权限（远程用户请配置免密 sudo）")


def service_active(name):
    proc = run_ok(["systemctl", "is-active", name], timeout=20)
    return (proc.stdout or "").strip() == "active"


def service_enabled(name):
    proc = run_ok(["systemctl", "is-enabled", name], timeout=20)
    return (proc.stdout or "").strip() in ("enabled", "enabled-runtime")


def systemctl(action, name, check=True, timeout=300):
    return run(["systemctl", action, name], timeout=timeout, check=check)


def daemon_reload():
    run(["systemctl", "daemon-reload"], timeout=120, check=True)


def ssl_context():
    """加载发行版系统 CA；便携 Python 的默认 CA 路径可能不包含 RHEL 的 bundle。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    ctx = ssl.create_default_context()
    paths = []
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = os.environ.get(key)
        if value and os.path.exists(value):
            paths.append(value)
    paths.extend(_CA_CANDIDATES)
    for path in paths:
        try:
            if os.path.isdir(path):
                ctx.load_verify_locations(capath=path)
            elif os.path.isfile(path) and os.path.getsize(path) > 0:
                ctx.load_verify_locations(cafile=path)
            else:
                continue
            debug("使用系统 CA：%s" % path)
            break
        except (OSError, ssl.SSLError) as exc:
            debug("加载 CA 失败 %s：%s" % (path, exc))
            continue
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _SSL_CONTEXT = ctx
    return ctx


def _curl_text(url, timeout=30):
    curl = shutil.which("curl")
    if not curl:
        raise ActionFailure("未找到 curl，无法回退下载")
    proc = run_ok([curl, "-fsSL", "--connect-timeout", "15",
                   "--max-time", str(timeout), url], timeout=timeout + 15)
    if proc.returncode != 0 or not proc.stdout:
        raise ActionFailure("curl 获取失败：%s" % url,
                            errors=[(proc.stderr or "").strip()[-500:]])
    return proc.stdout


def _curl_download(url, dest, timeout=90):
    curl = shutil.which("curl")
    if not curl:
        raise ActionFailure("未找到 curl，无法回退下载")
    tmp = dest + ".curl.part"
    remove_file(tmp)
    log("使用系统 curl 下载（系统 CA）：%s" % url)
    proc = run([curl, "-fL", "--retry", "2", "--connect-timeout", "20",
                "--max-time", str(max(timeout, 300)), "-o", tmp, url],
               timeout=max(timeout, 300) + 60, check=False)
    if proc.returncode != 0 or not os.path.exists(tmp):
        remove_file(tmp)
        raise ActionFailure("curl 下载失败：%s" % url,
                            errors=[(proc.stderr or "").strip()[-800:]])
    os.replace(tmp, dest)
    return dest


def download(url, dest, expected_sha256=None, timeout=90, retries=3):
    """下载文件并可选校验 SHA256；Python CA 失败时回退系统 curl。"""
    log("下载 %s" % url)
    ensure_dir(os.path.dirname(os.path.abspath(dest)), 0o755)
    last_error = None
    ctx = ssl_context()
    for attempt in range(1, retries + 1):
        tmp = dest + ".part"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "kubedeploy/%s" % os.environ.get("KUBEDEPLOY_VERSION", "1.0"),
            })
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last_report = 0
                while True:
                    chunk = resp.read(1024 * 128)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if done - last_report >= 10 * 1024 * 1024:
                        last_report = done
                        log("  已下载 %.1f MB%s" % (
                            done / 1024.0 / 1024.0,
                            (" / %.1f MB" % (total / 1024.0 / 1024.0)) if total else ""))
            os.replace(tmp, dest)
            if expected_sha256:
                actual = sha256_file(dest)
                if actual.lower() != expected_sha256.lower():
                    remove_file(dest)
                    raise ActionFailure("SHA256 校验失败：%s" % url,
                                        errors=["期望 %s，实际 %s" % (expected_sha256, actual)])
            return dest
        except Exception as exc:
            last_error = exc
            remove_file(tmp)
            log("下载失败（第 %d 次）：%s" % (attempt, exc))
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(exc, ssl.SSLError):
                log("Python CA 校验失败，改用系统 curl 下载")
                break
            time.sleep(min(5, attempt * 2))
    curl = shutil.which("curl")
    if curl:
        try:
            _curl_download(url, dest, timeout=timeout)
            if expected_sha256:
                actual = sha256_file(dest)
                if actual.lower() != expected_sha256.lower():
                    remove_file(dest)
                    raise ActionFailure("SHA256 校验失败：%s" % url,
                                        errors=["期望 %s，实际 %s" % (expected_sha256, actual)])
            return dest
        except Exception as exc:
            last_error = exc
            remove_file(dest)
    raise ActionFailure("下载失败：%s" % url, errors=[str(last_error)],
                        hint="请检查节点网络、DNS 或配置内部镜像")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_text(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kubedeploy/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        log("Python 获取失败，尝试系统 curl：%s" % exc)
        return _curl_text(url, timeout=timeout)


def safe_extract_tar(path, dest, strip_components=0):
    """安全解压：拒绝绝对路径/..，逐成员解压。"""
    ensure_dir(dest, 0o755)
    with tarfile.open(path, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            name = member.name
            if name.startswith("/") or ".." in name.split("/"):
                raise ActionFailure("压缩包包含不安全路径：%s" % name)
            if member.issym() or member.islnk():
                # 只允许相对、不越界的链接
                target = member.linkname
                if os.path.isabs(target) or ".." in target.split("/"):
                    raise ActionFailure("压缩包包含不安全链接：%s -> %s" % (name, target))
        for member in members:
            parts = member.name.split("/")
            if strip_components:
                if len(parts) <= strip_components:
                    continue
                member.name = "/".join(parts[strip_components:])
            tar.extract(member, dest)
    return dest


def read_os_release():
    data = {}
    text = read_file("/etc/os-release")
    if not text:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key] = value
    return data


def replace_lines(path, replacements):
    """按正则替换文本并写回，返回是否变化。"""
    text = read_file(path)
    new = text
    for pattern, repl in replacements:
        new = re.sub(pattern, repl, new, flags=re.MULTILINE)
    if new != text:
        write_file(path, new)
        return True
    return False


def wait_for(description, predicate, timeout=300, interval=4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:
            debug("等待 %s 时异常：%s" % (description, exc))
        time.sleep(interval)
    raise ActionFailure("等待%s超时（%ss）" % (description, timeout))


def get_state_dir():
    ensure_dir(AGENT_STATE_DIR, 0o700)
    return AGENT_STATE_DIR
