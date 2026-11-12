"""containerd/runc 静态安装、配置与 CRI 校验。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile

from .agent_base import (ActionFailure, backup_file, debug, download, ensure_dir,
                         fetch_text, log, read_file, run, run_ok, safe_extract_tar,
                         service_active, write_file)
from .constants import (CONTAINERD_SHA256, CONTAINERD_SOCK, DEFAULT_CONTAINERD_VERSION,
                        DEFAULT_REGISTRY_MIRRORS, DEFAULT_RUNC_VERSION,
                        RUNC_SHA256, RUNTIME_DOWNLOAD_MIRRORS)
from .util import parse_version_tuple


CONTAINERD_UNIT = """[Unit]
Description=containerd container runtime
Documentation=https://containerd.io
After=network.target local-fs.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/containerd
Type=notify
Delegate=yes
KillMode=mixed
Restart=always
RestartSec=5
LimitNPROC=infinity
LimitCORE=infinity
LimitNOFILE=1048576
TasksMax=infinity
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
"""


def _download_first(urls, dest, expected_sha256=None, timeout=180):
    last = None
    for url in urls:
        try:
            return download(url, dest, expected_sha256=expected_sha256,
                            timeout=timeout, retries=2)
        except Exception as exc:
            last = exc
            log("镜像下载失败，尝试下一个：%s" % exc)
    raise ActionFailure("所有下载源均失败", errors=[str(last)],
                        hint="请配置企业内网镜像，或检查 files.m.daocloud.io 连通性")


def _asset_urls(repo, tag, asset):
    urls = []
    for tpl in RUNTIME_DOWNLOAD_MIRRORS:
        urls.append(tpl.format(repo=repo, tag=tag, asset=asset))
    return urls


def _containerd_sha(version, arch):
    known = CONTAINERD_SHA256.get(version, {}).get(arch)
    if known:
        return known
    asset = "containerd-static-%s-linux-%s.tar.gz.sha256sum" % (version, arch)
    urls = _asset_urls("containerd/containerd", "v" + version, asset)
    for url in urls:
        try:
            text = fetch_text(url, timeout=20)
            m = re.search(r"([0-9a-fA-F]{64})", text)
            if m:
                return m.group(1).lower()
        except Exception:
            continue
    return None


def _runc_sha(version, arch):
    known = RUNC_SHA256.get(version, {}).get(arch)
    if known:
        return known
    asset = "runc.sha256sum"
    urls = _asset_urls("opencontainers/runc", "v" + version, asset)
    for url in urls:
        try:
            text = fetch_text(url, timeout=20)
            for line in text.splitlines():
                if line.strip().endswith("runc.%s" % arch):
                    return line.split()[0].lower()
        except Exception:
            continue
    return None


def _patch_containerd_config(text, sandbox_image=None, certs_path="/etc/containerd/certs.d"):
    changed = False
    if sandbox_image:
        pattern = r"(?m)^(\s*)sandbox_image\s*=\s*.*$"
        replacement = r'\1sandbox_image = "%s"' % sandbox_image
        new = re.sub(pattern, replacement, text)
        if new == text:
            # 不能直接追加到文件末尾：那会落到最后一个 TOML table 里，
            # containerd 会把它当成未知字段并拒绝启动。
            section = None
            for pat in (
                r"(?m)^\s*\[\s*plugins\s*\.\s*['\"]io\.containerd\.cri\.v1\.images['\"]\s*\]\s*$",
                r"(?m)^\s*\[\s*plugins\s*\.\s*['\"]io\.containerd\.grpc\.v1\.cri['\"]\s*\]\s*$",
            ):
                section = re.search(pat, text)
                if section:
                    break
            if section:
                insert = '\nsandbox_image = "%s"' % sandbox_image
                new = text[:section.end()] + insert + text[section.end():]
            else:
                # 某些 containerd 2.1 默认配置没有显式 images 表。
                parent = "[plugins.'io.containerd.cri.v1.images']"
                insert = '%s\n  sandbox_image = "%s"\n\n' % (parent, sandbox_image)
                first_images = re.search(
                    r"(?m)^.*io\.containerd\.cri\.v1\.images.*$", text)
                if first_images:
                    new = text[:first_images.start()] + insert + text[first_images.start():]
                else:
                    new = text.rstrip() + '\n\n' + insert
        changed = changed or (new != text)
        text = new
    # SystemdCgroup
    new = re.sub(r"(?m)^(\s*)SystemdCgroup\s*=\s*false\s*$",
                 r"\1SystemdCgroup = true", text)
    if new == text and "SystemdCgroup = true" not in text:
        # 追加到 runc options 段；若找不到，至少保留原有配置并稍后校验
        marker = "runtimes.runc.options"
        idx = text.find(marker)
        if idx >= 0:
            line_end = text.find("\n", idx)
            if line_end < 0:
                line_end = len(text)
            new = text[:line_end + 1] + "        SystemdCgroup = true\n" + text[line_end + 1:]
    changed = changed or (new != text)
    text = new
    # registry config_path
    for pat in (r"(?m)^(\s*)config_path\s*=\s*''\s*$",
                r'(?m)^(\s*)config_path\s*=\s*""\s*$'):
        new = re.sub(pat, r'\1config_path = "%s"' % certs_path, text)
        if new != text:
            changed = True
            text = new
    return text, changed


def _write_registry_mirrors():
    configs = {
        "docker.io": DEFAULT_REGISTRY_MIRRORS,
        "registry.k8s.io": ["https://k8s.m.daocloud.io"],
        "ghcr.io": ["https://ghcr.m.daocloud.io"],
        "quay.io": ["https://quay.m.daocloud.io"],
    }
    for registry, mirrors in configs.items():
        path = "/etc/containerd/certs.d/%s/hosts.toml" % registry
        lines = ['server = "https://%s"' % registry, ""]
        for mirror in mirrors:
            lines.append('[host."%s"]' % mirror)
            lines.append('  capabilities = ["pull", "resolve"]')
            lines.append("")
        write_file(path, "\n".join(lines))
    return list(configs)


def _wait_cri(timeout=90):
    import time
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        proc = run(["crictl", "--runtime-endpoint", CONTAINERD_SOCK, "info"],
                   timeout=30, check=False)
        if proc.returncode == 0:
            return True
        last = proc.stderr.strip()
        time.sleep(2)
    raise ActionFailure("CRI 接口不可用（crictl info 失败）",
                        errors=[last[-800:]],
                        hint="检查 /etc/containerd/config.toml 的 CRI 插件、SystemdCgroup 和 journalctl -u containerd")


def _verify_runtime():
    proc = run(["ctr", "version"], timeout=30, check=False)
    if proc.returncode != 0:
        raise ActionFailure("containerd 未正常运行：%s" % proc.stderr.strip())
    _wait_cri()
    dump = run(["containerd", "config", "dump"], timeout=60, check=False)
    if dump.returncode == 0 and "SystemdCgroup = true" not in (dump.stdout or ""):
        raise ActionFailure("containerd 未启用 SystemdCgroup，无法与 kubelet 的 systemd cgroup driver 匹配",
                            hint="检查 /etc/containerd/config.toml 并重启 containerd")
    return True


def install_runtime(params):
    arch = params.get("arch") or "amd64"
    containerd_version = params.get("containerd_version") or DEFAULT_CONTAINERD_VERSION
    runc_version = params.get("runc_version") or DEFAULT_RUNC_VERSION
    sandbox_image = params.get("sandbox_image") or ""
    manage = params.get("manage", True)
    log("安装 containerd %s / runc %s（%s）" % (containerd_version, runc_version, arch))

    if not os.path.exists("/usr/local/bin/containerd") or params.get("force_runtime"):
        c_sha = params.get("containerd_sha256") or _containerd_sha(containerd_version, arch)
        if not c_sha:
            raise ActionFailure("无法获取 containerd 校验和",
                                hint="请通过 --containerd-sha256 提供可信校验值")
        c_asset = "containerd-static-%s-linux-%s.tar.gz" % (containerd_version, arch)
        c_path = "/tmp/" + c_asset
        _download_first(_asset_urls("containerd/containerd", "v" + containerd_version, c_asset),
                        c_path, expected_sha256=c_sha, timeout=300)
        extract_dir = tempfile.mkdtemp(prefix="containerd-")
        try:
            safe_extract_tar(c_path, extract_dir)
            bin_dir = os.path.join(extract_dir, "bin")
            if not os.path.isdir(bin_dir):
                # 某些构建版本没有 bin/ 目录
                bin_dir = extract_dir
            for name in ("containerd", "containerd-shim-runc-v2", "containerd-shim",
                         "ctr", "containerd-stress"):
                src = os.path.join(bin_dir, name)
                if os.path.exists(src):
                    run(["install", "-m", "0755", src, "/usr/local/bin/" + name], timeout=30)
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
            try:
                os.unlink(c_path)
            except OSError:
                pass
    else:
        log("检测到 /usr/local/bin/containerd，保留现有二进制")

    if not os.path.exists("/usr/local/bin/runc") or params.get("force_runtime"):
        r_sha = params.get("runc_sha256") or _runc_sha(runc_version, arch)
        if not r_sha:
            raise ActionFailure("无法获取 runc 校验和")
        r_path = "/tmp/runc.%s" % arch
        _download_first(_asset_urls("opencontainers/runc", "v" + runc_version, "runc.%s" % arch),
                        r_path, expected_sha256=r_sha, timeout=300)
        run(["install", "-m", "0755", r_path, "/usr/local/bin/runc"], timeout=30)
        try:
            os.unlink(r_path)
        except OSError:
            pass
    else:
        log("检测到 /usr/local/bin/runc，保留现有二进制")

    if shutil.which("restorecon"):
        run_ok(["restorecon", "-R", "/usr/local/bin"], timeout=60)

    ensure_dir("/etc/containerd/certs.d")
    config_path = "/etc/containerd/config.toml"
    default_cfg = run(["containerd", "config", "default"], timeout=60, check=False)
    if default_cfg.returncode != 0 or not default_cfg.stdout.strip():
        raise ActionFailure("无法生成 containerd 默认配置",
                            errors=[default_cfg.stderr.strip()[-800:]])
    patched, changed = _patch_containerd_config(default_cfg.stdout, sandbox_image=sandbox_image)
    if changed or not os.path.exists(config_path) or params.get("force_runtime"):
        backup_file(config_path)
        write_file(config_path, patched, 0o644)
        config_changed = True
    else:
        config_changed = False
    mirrors = _write_registry_mirrors()
    write_file("/etc/crictl.yaml", "runtime-endpoint: %s\nimage-endpoint: %s\ntimeout: 30\n" % (
        CONTAINERD_SOCK, CONTAINERD_SOCK))
    write_file("/etc/systemd/system/containerd.service", CONTAINERD_UNIT, 0o644)
    run(["systemctl", "daemon-reload"], timeout=60)
    run(["systemctl", "enable", "containerd"], timeout=60, check=False)
    if service_active("containerd") and not config_changed:
        log("containerd 已运行且配置未变化")
    else:
        run(["systemctl", "restart", "containerd"], timeout=180, check=False)
    if not service_active("containerd"):
        raise ActionFailure("containerd 启动失败",
                            hint="执行 journalctl -u containerd -n 100 查看原因")
    _verify_runtime()
    return {
        "containerd_version": _runtime_version("containerd"),
        "runc_version": _runtime_version("runc"),
        "config": config_path,
        "sandbox_image": sandbox_image,
        "registry_mirrors": mirrors,
        "changed": config_changed,
    }


def _runtime_version(binary):
    proc = run_ok([binary, "--version"], timeout=20)
    text = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"v?([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else ""


def set_sandbox_image(params):
    image = params.get("pause_image") or ""
    if not image:
        raise ActionFailure("未提供 pause 镜像")
    path = "/etc/containerd/config.toml"
    text = read_file(path)
    if not text:
        raise ActionFailure("containerd 配置不存在：%s" % path)
    new, changed = _patch_containerd_config(text, sandbox_image=image)
    backup = None
    if changed:
        backup = backup_file(path)
        write_file(path, new, 0o644)
        run(["systemctl", "restart", "containerd"], timeout=180, check=False)
        if not service_active("containerd"):
            journal = run_ok(["journalctl", "-u", "containerd", "-n", "100",
                              "--no-pager"], timeout=30)
            if backup:
                shutil.copy2(backup, path)
                run(["systemctl", "restart", "containerd"], timeout=180, check=False)
            raise ActionFailure(
                "修改 sandbox_image 后 containerd 启动失败",
                errors=[(journal.stdout or journal.stderr or "")[-2000:]],
                hint="已尝试恢复备份配置；检查 journalctl -u containerd -n 100")
    _wait_cri()
    return {"sandbox_image": image, "changed": changed}
