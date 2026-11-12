"""基于系统 OpenSSH 的远程传输层，支持密钥/密码、sudo 与流式日志。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time

from .constants import REMOTE_LIB, REMOTE_PYTHON, REMOTE_ROOT
from .errors import TransportError, ValidationError
from .models import SshOptions
from .runner import ProcessTimeout, run_process
from .util import normalize_address, shell_join


def _require_ssh():
    path = shutil.which("ssh")
    if not path:
        raise TransportError("未找到 ssh 命令，请先安装 OpenSSH 客户端")
    return path


def quote(value):
    return shlex.quote(str(value))


def _ssh_destination(user, host):
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return "%s@%s" % (user, host) if user else host


class SshTransport:
    """一条到目标节点的 SSH 传输通道。

    不支持 root 登录时，通过免密 sudo 执行特权命令。密码只保存在内存和
    临时 askpass 文件（0600）中，不写入日志、状态文件或子进程参数。
    """

    def __init__(self, address, options, console=None, dry_run=False):
        self.address = normalize_address(address)
        self.options = options
        self.console = console
        self.dry_run = dry_run
        self.ssh = _require_ssh()
        self._askpass_dir = None
        self._askpass_script = None
        self._password_file = None
        self._checked = False
        self._uid = None
        self._sudo_prefix = ""
        if not self.options.known_hosts and self.options.strict_host_key_checking == "accept-new":
            try:
                ssh_dir = os.path.expanduser("~/.ssh")
                os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
                os.chmod(ssh_dir, 0o700)
            except OSError:
                pass
        self._base = self._build_base()

    def _build_base(self):
        opts = self.options
        args = [self.ssh, "-T", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=%d" % int(opts.connect_timeout),
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8",
                "-o", "StrictHostKeyChecking=%s" % opts.strict_host_key_checking,
                "-o", "RequestTTY=no"]
        if opts.known_hosts:
            args += ["-o", "UserKnownHostsFile=%s" % opts.known_hosts]
        if opts.key:
            args += ["-i", os.path.expanduser(opts.key), "-o", "IdentitiesOnly=yes"]
        if opts.port:
            args += ["-p", str(opts.port)]
        if opts.proxy_jump:
            args += ["-J", opts.proxy_jump]
        for extra in opts.extra_options:
            args += ["-o", extra]
        if not opts.password:
            args += ["-o", "BatchMode=yes"]
            args += ["-o", "NumberOfPasswordPrompts=1"]
        else:
            args += ["-o", "NumberOfPasswordPrompts=1"]
            args += ["-o", "PreferredAuthentications=password,keyboard-interactive"]
        args.append(_ssh_destination(opts.user, self.address))
        return args

    def _ensure_askpass(self):
        if self._askpass_script or not self.options.password:
            return
        if os.name != "posix":
            # Windows OpenSSH 也支持 SSH_ASKPASS；使用临时脚本（.cmd）不可靠，
            # 因此 Windows 控制端建议使用密钥认证。
            raise TransportError("Windows 控制端暂不支持密码认证，请使用 SSH 密钥或 ssh-agent")
        d = tempfile.mkdtemp(prefix="kubedeploy-askpass-")
        os.chmod(d, 0o700)
        pfile = os.path.join(d, "password")
        script = os.path.join(d, "askpass.sh")
        with open(pfile, "w", encoding="utf-8") as fh:
            fh.write(self.options.password + "\n")
        os.chmod(pfile, 0o600)
        body = '#!/bin/sh\nexec cat "$KUBEDEPLOY_ASKPASS_FILE"\n'
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(script, 0o700)
        self._askpass_dir = d
        self._askpass_script = script
        self._password_file = pfile

    def _env(self):
        env = os.environ.copy()
        env.setdefault("LANG", "C.UTF-8")
        if self.options.password:
            self._ensure_askpass()
            env["SSH_ASKPASS"] = self._askpass_script
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["KUBEDEPLOY_ASKPASS_FILE"] = self._password_file
            env["DISPLAY"] = env.get("DISPLAY", ":0")
        return env

    def close(self):
        if self._askpass_dir:
            try:
                shutil.rmtree(self._askpass_dir, ignore_errors=True)
            except Exception:
                pass
            self._askpass_dir = None
            self._askpass_script = None
            self._password_file = None

    def _command(self, remote_command, sudo=False):
        prefix = ""
        if sudo:
            prefix = "sudo -n "
        return prefix + remote_command

    def run(self, remote_command, input_data=None, timeout=900, sudo=None,
            stream=False, prefix="", check=False):
        """执行远程 shell 命令。sudo=None 时根据已探测身份自动决定。"""
        if self.dry_run:
            if self.console:
                self.console.debug("[dry-run] ssh %s: %s" % (self.address, remote_command))
            return _DryResult()
        if sudo is None:
            sudo = self.need_sudo()
        cmd = self._command(remote_command, sudo=sudo)
        callback = None
        if stream and self.console:
            callback = lambda line: self.console.remote(self.address, line)
        result = run_process(self._base + [cmd], input_data=input_data,
                             timeout=timeout, env=self._env(),
                             stream_stderr=callback if stream else None,
                             stream_stdout=None, prefix="%s: " % self.address)
        if check and not result.ok:
            raise TransportError("远程命令失败（%s，rc=%s）：%s" % (
                self.address, result.rc, result.summary()))
        return result

    def test_connection(self):
        result = self.run("printf __KD_OK__", timeout=min(45, self.options.connect_timeout + 15),
                          sudo=False)
        if not result.ok or "__KD_OK__" not in result.stdout:
            msg = result.stderr.strip() or result.stdout.strip() or "未知错误"
            raise TransportError("无法通过 SSH 连接 %s：%s" % (self.address, msg),
                                 hint="请检查网络、SSH 端口/用户/密钥；如使用密码认证，"
                                      "可改用密钥或 ssh-agent")
        self._checked = True
        return True

    def need_sudo(self):
        if self._uid is None:
            self._uid = self._detect_uid()
        if self._uid == 0:
            return False
        if not self.options.become:
            raise TransportError("远程用户不是 root 且已禁用 sudo（--no-become）")
        test = run_process(self._base + ["sudo -n true"], timeout=30,
                           env=self._env(), prefix="%s: " % self.address)
        if not test.ok:
            raise TransportError(
                "远程用户非 root，且免密 sudo 不可用（%s）" % self.address,
                hint="请使用 root，或为 SSH 用户配置 NOPASSWD sudo",
            )
        return True

    def _detect_uid(self):
        result = self.run("id -u", timeout=30, sudo=False)
        if not result.ok:
            raise TransportError("无法在 %s 上执行 id -u：%s" % (self.address, result.summary()))
        try:
            return int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise TransportError("无法解析远程 UID：%r" % result.stdout)

    def detect_base(self):
        result = self.run("uname -m; id -u; command -v python3 || true; command -v tar || true",
                          timeout=30, sudo=False)
        if not result.ok:
            raise TransportError("远程基础探测失败：%s" % result.summary())
        lines = [x.strip() for x in result.stdout.splitlines() if x.strip()]
        arch = lines[0] if lines else ""
        uid = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None
        python = lines[2] if len(lines) > 2 else ""
        tar = lines[3] if len(lines) > 3 else ""
        self._uid = uid
        return {"arch": arch, "uid": uid, "python": python, "tar": tar}

    def __repr__(self):
        return "SshTransport(%s)" % self.address


class _DryResult:
    rc = 0
    ok = True
    stdout = ""
    stderr = ""
    duration = 0

    def summary(self, max_chars=1000):
        return ""
