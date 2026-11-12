"""远程节点准备与 Python agent 调用。

- 远程：OpenSSH 连接 -> 免密 sudo（如需）-> 检查/引导 Python 3.8+ ->
  上传 agent 包 -> 通过 stdin 发送 JSON 请求并运行 Python agent。
- 本地：无需 SSH，直接调用同一 agent 包。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
import time

from .constants import (APP_NAME, PYTHON_ASSET, PYTHON_BUILD, PYTHON_DOWNLOAD_MIRRORS,
                        PYTHON_SHA256, PYTHON_VERSION, REMOTE_LIB, REMOTE_PYTHON,
                        REMOTE_ROOT, VERSION)
from .errors import AgentError, TransportError, ValidationError
from .models import SshOptions
from .runner import ProcessTimeout, run_process, run_shell
from .ssh import SshTransport, quote
from .util import json_dumps, normalize_address, tail


PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_CANDIDATES = ["python3", "python3.13", "python3.12", "python3.11",
                     "python3.10", "python3.9", "python3.8"]


def _source_files():
    for root, dirs, files in os.walk(os.path.join(PACKAGE_ROOT, "kubedeploy")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)
            elif name.endswith((".yaml", ".yml")) and os.sep + "resources" + os.sep in root + os.sep:
                yield os.path.join(root, name)


def build_id():
    h = hashlib.sha256()
    for path in sorted(_source_files()):
        h.update(os.path.basename(path).encode())
        with open(path, "rb") as fh:
            h.update(fh.read())
    h.update(VERSION.encode())
    return h.hexdigest()[:16]


def build_agent_package():
    """生成上传到节点的 tar.gz，顶层为 kubedeploy/ 与 VERSION。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(_source_files()):
            arc = os.path.join("kubedeploy", os.path.relpath(path, os.path.join(PACKAGE_ROOT, "kubedeploy")))
            arc = arc.replace(os.sep, "/")
            info = tar.gettarinfo(path, arcname=arc)
            info.mode = 0o644
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
        data = ("%s %s\n" % (VERSION, build_id())).encode()
        info = tarfile.TarInfo("VERSION")
        info.size = len(data)
        info.mode = 0o644
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class LocalTransport:
    """本地 root/sudo 执行。仅支持 Linux（本地 Kubernetes 部署）。"""

    def __init__(self, console=None, dry_run=False, address="localhost"):
        self.console = console
        self.dry_run = dry_run
        self.address = address or "localhost"
        self._uid = None

    def close(self):
        pass

    def _sudo(self):
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise TransportError("本地部署仅支持 Linux；macOS/Windows 请连接远程 Linux 节点")
        self._uid = os.geteuid()
        if self._uid == 0:
            return False
        if not shutil.which("sudo"):
            raise TransportError("本地部署需要 root 权限，且未找到 sudo")
        test = run_process(["sudo", "-n", "true"], timeout=10)
        if not test.ok:
            raise TransportError("本地用户不是 root，且 sudo 需要密码",
                                 hint="请以 root 运行，或配置 NOPASSWD sudo 后重试")
        return True

    def run(self, command, input_data=None, timeout=900, sudo=None, stream=False, prefix="", check=False):
        if self.dry_run:
            return _DryResult()
        if sudo is None:
            sudo = self._sudo()
        full = ("sudo -n " if sudo else "") + command
        callback = None
        if stream and self.console:
            callback = lambda line: self.console.remote(self.address, line)
        result = run_shell(full, input_data=input_data, timeout=timeout,
                           stream_stderr=callback if stream else None,
                           prefix="%s: " % self.address)
        if check and not result.ok:
            raise TransportError("本地命令失败（rc=%s）：%s" % (result.rc, result.summary()))
        return result

    def test_connection(self):
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise TransportError("本地部署仅支持 Linux")
        self._sudo()
        return True

    def need_sudo(self):
        return self._sudo()

    def detect_base(self):
        return {"arch": os.uname().machine, "uid": os.geteuid(),
                "python": sys.executable, "tar": shutil.which("tar") or ""}


def _python_probe_command():
    candidates = " ".join(PYTHON_CANDIDATES)
    return r'''
found=""
for p in %s; do
  if command -v "$p" >/dev/null 2>&1; then
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      found=$(command -v "$p")
      break
    fi
  fi
done
if [ -n "$found" ]; then
  printf 'PYTHON=%%s\n' "$found"
  exit 0
fi
if [ -x %s/bin/python3 ] && %s/bin/python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  printf 'PYTHON=%%s\n' %s/bin/python3
  exit 0
fi
exit 2
''' % (candidates, REMOTE_PYTHON, REMOTE_PYTHON, REMOTE_PYTHON)


def _bootstrap_script():
    # URL/SHA/ASSET 由 _bootstrap_script_for 按远端架构填充。
    return r'''#!/bin/sh
set -eu
ARCH="$1"
case "$ARCH" in
  x86_64|amd64) PARCH=amd64 ;;
  aarch64|arm64) PARCH=arm64 ;;
  *) echo "不支持的架构: $ARCH" >&2; exit 3 ;;
esac
BASE=/opt/kube-deploy
DEST="$BASE/python"
if [ -x "$DEST/bin/python3" ] && "$DEST/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  exit 0
fi
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  echo "缺少 curl/wget，且未找到可用 Python 3.8+" >&2; exit 4
fi
command -v tar >/dev/null 2>&1 || { echo "缺少 tar" >&2; exit 4; }
mkdir -p "$BASE"
TMP=$(mktemp /tmp/kubedeploy-python.XXXXXX)
trap 'rm -f "$TMP"' EXIT
URLS="__URLS__"
SHA="__SHA__"
ASSET="__ASSET__"
ok=0
for u in $URLS; do
  case "$u" in
    *__ASSET__*) u=$(printf '%s' "$u" | sed "s|__ASSET__|$ASSET|g") ;;
  esac
  echo "下载便携 Python: $u" >&2
  if command -v curl >/dev/null 2>&1; then
    DL_OK=0
    curl -fL --retry 3 --connect-timeout 20 -o "$TMP" "$u" || DL_OK=$?
  else
    DL_OK=0
    wget -q -T 30 -O "$TMP" "$u" || DL_OK=$?
  fi
  if [ "$DL_OK" -eq 0 ]; then
    if echo "$SHA  $TMP" | sha256sum -c - >/dev/null 2>&1; then
      ok=1
      break
    else
      echo "SHA256 校验失败" >&2
    fi
  fi
done
[ "$ok" -eq 1 ] || { echo "无法下载便携 Python，请检查网络或预装 Python 3.8+" >&2; exit 5; }
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
tar -xzf "$TMP" -C "$DEST.new" --strip-components=1
"$DEST.new/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
echo "便携 Python 已安装到 $DEST/bin/python3" >&2
'''


def _bootstrap_script_for(arch):
    amd_arch = "amd64" if arch in ("x86_64", "amd64") else "arm64"
    asset = PYTHON_ASSET[amd_arch]
    # URL 不含空白字符，直接拼接；若用 shlex.quote，引号会作为字面量进入
    # 变量展开后的 for 循环，导致 curl 收到带单引号的 URL。
    urls = " ".join(u.format(build=PYTHON_BUILD, asset=asset)
                    for u in PYTHON_DOWNLOAD_MIRRORS)
    text = _bootstrap_script()
    text = text.replace("__URLS__", urls)
    text = text.replace("__SHA__", PYTHON_SHA256[amd_arch])
    text = text.replace("__ASSET__", asset)
    return text


class NodeAgentClient:
    """单个节点的 agent 生命周期与 RPC。"""

    def __init__(self, address, ssh_options=None, console=None, dry_run=False,
                 local=None, force_upload=False):
        self.address = normalize_address(address)
        self.ssh_options = ssh_options or SshOptions()
        self.console = console
        self.dry_run = dry_run
        self.force_upload = force_upload
        self.local = local
        if local is None:
            self.local = self._is_local_hint()
        self.transport = LocalTransport(console, dry_run, address=self.address) if self.local else SshTransport(
            self.address, self.ssh_options, console, dry_run)
        self.python = sys.executable if self.local else None
        self.base = None
        self._prepared = False
        self._remote_uid = None
        self._sudo = None

    def _is_local_hint(self):
        try:
            return self.address.lower() in ("localhost", "127.0.0.1", "::1")
        except Exception:
            return False

    def close(self):
        try:
            self.transport.close()
        except Exception:
            pass

    def prepare(self):
        if self.dry_run:
            self._prepared = True
            return
        if self._prepared:
            return
        self.transport.test_connection()
        base = self.transport.detect_base()
        self._remote_uid = base.get("uid")
        arch = base.get("arch") or "unknown"
        if arch not in ("x86_64", "amd64", "aarch64", "arm64"):
            raise TransportError("节点 %s 架构不受支持：%s" % (self.address, arch),
                                 hint="仅支持 x86_64/amd64 与 aarch64/arm64")
        self._sudo = self.transport.need_sudo()
        if self.local:
            self.python = sys.executable
            self.base = PACKAGE_ROOT
        else:
            self._ensure_remote_python(arch)
            self._ensure_remote_package()
        self._prepared = True

    def _ensure_remote_python(self, arch):
        probe = self.transport.run(_python_probe_command(), timeout=60, sudo=False)
        if probe.ok:
            for line in probe.stdout.splitlines():
                if line.startswith("PYTHON="):
                    self.python = line.split("=", 1)[1].strip()
                    return
        if not probe.ok and probe.rc not in (1, 2):
            raise TransportError("无法检查 %s 的 Python：%s" % (self.address, probe.summary()))
        self.console.warn("%s 未找到 Python 3.8+，正在安装便携 Python（约 30 MB）" % self.address)
        script = _bootstrap_script_for(arch)
        result = self.transport.run("sh -s -- %s" % shlex.quote(arch),
                                    input_data=script, timeout=900, sudo=self._sudo,
                                    stream=True)
        if not result.ok:
            raise TransportError("节点 %s 的 Python 引导失败：%s" % (self.address, result.summary()),
                                 hint="可预先安装 Python 3.8+，或检查节点到 files.m.daocloud.io 的网络")
        self.python = REMOTE_PYTHON + "/bin/python3"

    def _ensure_remote_package(self):
        expected = "%s %s" % (VERSION, build_id())
        current = self.transport.run("cat %s/VERSION 2>/dev/null || true" % REMOTE_LIB,
                                     timeout=30, sudo=self._sudo)
        if not self.force_upload and current.stdout.strip() == expected:
            self.base = REMOTE_LIB
            return
        self.console.info("上传 KubeDeploy agent 到 %s ..." % self.address)
        data = build_agent_package()
        script = r'''set -eu
base=%s
rm -rf "$base/lib.new"
mkdir -p "$base/lib.new"
tar -xzf - -C "$base/lib.new"
test -f "$base/lib.new/VERSION"
rm -rf "$base/lib.old"
if [ -d "$base/lib" ]; then mv "$base/lib" "$base/lib.old"; fi
mv "$base/lib.new" "$base/lib"
rm -rf "$base/lib.old"
''' % REMOTE_ROOT
        result = self.transport.run("sh -c %s" % shlex.quote(script),
                                    input_data=data, timeout=180, sudo=self._sudo)
        if not result.ok:
            raise TransportError("上传 agent 失败（%s）：%s" % (self.address, result.summary()))
        check = self.transport.run("cat %s/VERSION" % REMOTE_LIB, timeout=30, sudo=self._sudo)
        if check.stdout.strip() != expected:
            raise TransportError("agent 包校验失败：期望 %r，实际 %r" % (
                expected, check.stdout.strip()))
        self.base = REMOTE_LIB

    def _agent_command(self):
        if self.local:
            env = "PYTHONPATH=%s PYTHONUNBUFFERED=1 KUBEDEPLOY_AGENT=1" % shlex.quote(self.base)
            return "env %s %s -B -m kubedeploy.agent --stdin" % (
                env, shlex.quote(self.python))
        env = ("PYTHONPATH=%s PYTHONUNBUFFERED=1 KUBEDEPLOY_AGENT=1" % REMOTE_LIB)
        return "env %s %s -B -m kubedeploy.agent --stdin" % (
            env, shlex.quote(self.python))

    def call(self, action, params=None, timeout=1800, stream=True, check=True):
        payload = {"action": action, "params": params or {}}
        if self.dry_run:
            if self.console:
                self.console.debug("[dry-run] agent %s: %s %s" % (
                    self.address, action, json_dumps(params or {})))
            return {"ok": True, "dry_run": True, "action": action, "data": {}}
        self.prepare()
        result = self.transport.run(self._agent_command(),
                                    input_data=json_dumps(payload).encode("utf-8"),
                                    timeout=timeout, sudo=self._sudo,
                                    stream=stream)
        text = (result.stdout or "").strip()
        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                # 兼容 stderr 混入 stdout 的情况：从后往前找 JSON 行
                for line in reversed(text.splitlines()):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            parsed = json.loads(line)
                            break
                        except ValueError:
                            continue
        if parsed is None:
            msg = result.stderr.strip() or text or "agent 没有返回 JSON"
            raise AgentError("节点 %s 的 agent 调用失败（rc=%s）：%s" % (
                self.address, result.rc, tail(msg, 30)), action=action)
        if result.rc != 0 or not parsed.get("ok", False):
            errors = parsed.get("errors") or []
            if not errors and parsed.get("error"):
                errors = [parsed["error"]]
            message = "；".join(str(x) for x in errors) or tail(result.stderr, 20) or "未知错误"
            raise AgentError("节点 %s 执行 %s 失败：%s" % (self.address, action, message),
                             action=action, details=errors,
                             hint=parsed.get("hint"))
        return parsed.get("data", {})

    def shell(self, command, timeout=120, sudo=None, stream=False):
        self.prepare()
        return self.transport.run(command, timeout=timeout, sudo=sudo, stream=stream)


class _DryResult:
    rc = 0
    ok = True
    stdout = ""
    stderr = ""
    duration = 0

    def summary(self, max_chars=1000):
        return ""
