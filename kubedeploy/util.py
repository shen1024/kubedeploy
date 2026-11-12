"""无第三方依赖的通用工具。"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def json_dumps(obj, pretty=False):
    return json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None,
                      sort_keys=pretty, default=str)


def read_json(path, default=None):
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    except (ValueError, OSError) as exc:
        raise ValueError("无法读取 JSON 文件 %s: %s" % (path, exc))


def atomic_write(path, content, mode=0o600):
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent, text=False)
    try:
        with os.fdopen(fd, "wb") as fh:
            if isinstance(content, str):
                content = content.encode("utf-8")
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def ensure_dir(path, mode=0o755):
    path = os.fspath(path)
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def b64e(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def b64d(text):
    return base64.b64decode(text.encode("ascii"))


def safe_slug(value, default="k8s"):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9.-]+", "-", value)
    value = value.strip(".-")
    if not value:
        value = default
    return value[:63]


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_ipv4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def normalize_address(value):
    """校验并规范化节点地址；允许 IPv4/IPv6 或 DNS 名称。"""
    value = (value or "").strip()
    if not value:
        raise ValueError("节点地址不能为空")
    if " " in value or "/" in value or "@" in value:
        raise ValueError("非法节点地址: %s" % value)
    if value.lower() == "localhost":
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if len(value) > 253 or not re.match(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$", value):
        raise ValueError("非法节点地址: %s" % value)
    return value


def split_tokens(raw):
    """只按分隔符拆分节点表达式，不校验；支持 ssh地址=节点私网IP。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = re.split(r"[,，\s]+", raw or "")
    return [item.strip() for item in items if item and item.strip()]


def split_addresses(raw):
    return [normalize_address(item) for item in split_tokens(raw)]


def cidr_network(value):
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError("非法 CIDR %s: %s" % (value, exc))


def shell_join(cmd):
    if isinstance(cmd, str):
        return cmd
    return " ".join(shlex.quote(str(x)) for x in cmd)


def parse_kv_output(text):
    result = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            result[key.strip()] = val.strip()
    return result


def tail(text, lines=40, max_chars=12000):
    text = text or ""
    parts = text.splitlines()
    if len(parts) > lines:
        parts = parts[-lines:]
    out = "\n".join(parts)
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out


def redact(text, secrets):
    if not text:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def parse_version_tuple(value):
    """将 1.34.2 / v1.34.2 转为 (1,34,2)，失败返回 None。"""
    if value is None:
        return None
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", str(value).strip())
    if not m:
        return None
    return tuple(int(x) if x is not None else 0 for x in m.groups())


def compare_versions(a, b):
    """语义化比较 1.2.3；返回 -1/0/1。"""
    pa = parse_version_tuple(a)
    pb = parse_version_tuple(b)
    if pa is None or pb is None:
        raise ValueError("非法版本号: %s / %s" % (a, b))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def package_version_key(value):
    """用于包版本排序，兼容 1.34.2-1.1 / 1.34.2-150500.1.1。"""
    value = str(value)
    nums = [int(x) for x in re.findall(r"\d+", value)]
    return tuple(nums)


def human_bytes(num):
    num = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return "%3.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f PiB" % num


def run_local(cmd, check=False, timeout=60, env=None, cwd=None, input_data=None):
    """运行本地命令，返回 CompletedProcess（文本）。仅用于 CLI 侧。"""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "cwd": cwd,
        "env": env,
    }
    if input_data is not None:
        kwargs["input"] = input_data
    proc = subprocess.run(cmd, text=True, errors="replace", **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError("命令失败(%s): %s\n%s" % (
            proc.returncode, shell_join(cmd), tail(proc.stderr)))
    return proc


def command_exists(name):
    from shutil import which
    return which(name) is not None


def local_ips():
    """返回本机所有非回环 IPv4/IPv6 地址。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr:
                ips.add(addr)
    except OSError:
        pass
    try:
        if command_exists("ip"):
            out = run_local(["ip", "-o", "addr", "show"], timeout=10).stdout
            for line in out.splitlines():
                m = re.search(r"\sinet6?\s+([0-9a-fA-F:.]+)/", line)
                if m:
                    ips.add(m.group(1))
    except Exception:
        pass
    try:
        out = run_local(["hostname", "-I"], timeout=5).stdout
        for item in out.split():
            ips.add(item)
    except Exception:
        pass
    ips.discard("")
    return ips


def is_local_address(address):
    if not address:
        return False
    if address.lower() in ("localhost", "127.0.0.1", "::1"):
        return True
    ips = local_ips()
    if address in ips:
        return True
    try:
        resolved = {x[4][0] for x in socket.getaddrinfo(address, None)}
        return bool(resolved & ips)
    except OSError:
        return False


def free_port(port, host="127.0.0.1"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_until(predicate, timeout=300, interval=3, description="条件"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError("等待%s超时（%ss）" % (description, timeout))


def version_from_image(image):
    """从镜像引用中提取版本（用于 pause）。"""
    if not image:
        return ""
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        return last.split(":", 1)[1]
    return ""


def valid_dns_label(name):
    return bool(re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name or "")) and len(name) <= 63


def valid_dns_subdomain(name):
    """Kubernetes 节点名要求：小写 DNS subdomain，≤253 字符。"""
    name = (name or "").strip()
    if not name or len(name) > 253:
        return False
    return all(valid_dns_label(part) for part in name.split("."))


def sanitize_node_name(hostname, node_ip):
    """把 localhost/非法主机名转换为稳定、唯一的 Kubernetes 节点名。

    返回小写 DNS subdomain。真实主机名合法时尽量保留；localhost 系列、
    大写/下划线/空格等不合法值使用 node-<ip>。
    """
    raw = (hostname or "").strip()
    candidate = raw.lower()
    if (candidate and not candidate.startswith("localhost")
            and valid_dns_subdomain(candidate)):
        return candidate
    suffix = re.sub(r"[^a-z0-9-]+", "-", (node_ip or "").lower()).strip("-")
    if not suffix:
        suffix = "node"
    return ("node-" + suffix)[:63].strip("-")
