"""终端交互、日志与颜色。"""

from __future__ import annotations

import getpass
import os
import sys
import textwrap
import time

from .constants import APP_NAME, VERSION


class Console:
    def __init__(self, verbose=False, color=None, log_file=None):
        self.verbose = verbose
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = bool(color)
        self.log_file = log_file
        self._log_handle = None
        if log_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
                fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.chmod(log_file, 0o600)
                except OSError:
                    pass
                self._log_handle = os.fdopen(fd, "a", encoding="utf-8")
            except OSError as exc:
                self._log_handle = None
                self.warn("无法创建日志文件 %s：%s（继续但不落盘）" % (log_file, exc))
        self._last_progress = 0

    def close(self):
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def _paint(self, code, text):
        if not self.color:
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    def _write(self, text, err=False):
        stream = sys.stderr if err else sys.stdout
        print(text, file=stream, flush=True)
        if self._log_handle:
            self._log_handle.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
            self._log_handle.flush()

    def raw(self, text):
        self._write(text)

    def banner(self):
        self._write(self._paint("36;1", "%s  v%s" % (APP_NAME, VERSION)))
        self._write("企业级 Kubernetes 一键部署 / 扩缩容 / 升级 / 回滚工具")
        self._write("支持 Kubernetes v1.28 - v1.37 | amd64/arm64 | CentOS 7-10 / Ubuntu 20.04-26.04")
        self._write("")

    def section(self, title):
        self._write("")
        self._write(self._paint("36;1", "== %s ==" % title))

    def step(self, message):
        self._write("%s %s" % (self._paint("34;1", "[*]"), message))

    def info(self, message):
        self._write("%s %s" % (self._paint("36", "[i]"), message))

    def success(self, message):
        self._write("%s %s" % (self._paint("32;1", "[+]"), message))

    def warn(self, message):
        self._write("%s %s" % (self._paint("33;1", "[!]"), message), err=True)

    def error(self, message):
        self._write("%s %s" % (self._paint("31;1", "[-]"), message), err=True)

    def debug(self, message):
        if self.verbose:
            self._write("%s %s" % (self._paint("90", "[d]"), message), err=True)

    def remote(self, node, line):
        text = line.rstrip()
        if not text:
            return
        self._write("%s %s" % (self._paint("90", "  [%s]" % node), text))

    def command(self, node, message):
        self._write("%s %s" % (self._paint("35", "  [%s]" % node), message))

    def ask(self, prompt, default=None, secret=False):
        suffix = " [%s]" % default if default not in (None, "") else ""
        full = "%s%s: " % (prompt, suffix)
        if secret:
            value = getpass.getpass(full)
        else:
            try:
                value = input(full)
            except EOFError:
                value = ""
        value = value.strip()
        if not value and default is not None:
            return default
        return value

    def confirm(self, prompt, default=False, assume_yes=False):
        if assume_yes:
            self.info("%s -> 是（--yes）" % prompt)
            return True
        hint = "Y/n" if default else "y/N"
        try:
            value = input("%s [%s]: " % (prompt, hint)).strip().lower()
        except EOFError:
            return default
        if not value:
            return default
        return value in ("y", "yes", "是", "确认")

    def require_phrase(self, prompt, phrase, assume_yes=False):
        if assume_yes:
            self.warn("%s -> 已由 --yes 自动确认（危险操作）" % prompt)
            return True
        try:
            value = input("%s\n请输入 %s 继续: " % (prompt, phrase))
        except EOFError:
            return False
        return value.strip() == phrase

    def table(self, rows, headers=None):
        rows = [list(map(str, row)) for row in rows]
        if headers:
            rows = [list(map(str, headers))] + rows
        widths = [0] * max((len(r) for r in rows), default=0)
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        for idx, row in enumerate(rows):
            line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
            self._write(line)
            if headers and idx == 0:
                self._write("  ".join("-" * widths[i] for i in range(len(widths))))

    def wrap(self, text, width=88):
        for line in textwrap.wrap(text, width=width):
            self._write(line)
