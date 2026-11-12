"""Kubernetes 软件包源：阿里云优先，缺失版本可受控回退官方源。"""

from __future__ import annotations

import os
import re
import shutil
import time

from .agent_base import (ActionFailure, backup_file, debug, download, ensure_dir,
                         fetch_text, log, read_file, read_os_release, run, run_ok,
                         write_file)
from .agent_platform import detect
from . import repository
from .constants import DEFAULT_KUBE_REPO_BASE
from .util import package_version_key, parse_version_tuple
from .versions import VersionRequest, select_package_version


APT_TMP = "/var/tmp"


def _apt_env():
    env = os.environ.copy()
    env["TMPDIR"] = APT_TMP
    env["TMP"] = APT_TMP
    env["TEMP"] = APT_TMP
    return env


def _apt_cmd(args):
    # 某些 Ubuntu 镜像的 /tmp 被塞满或权限异常，apt-key 无法创建
    # /tmp/apt.conf.*；另外 unattended-upgrades 常持有 dpkg 锁。
    return ["apt-get",
            "-o", "Dir::Temp=%s" % APT_TMP,
            "-o", "DPkg::Lock::Timeout=600"] + list(args)


def _repo_available(repo_url, kind):
    if kind == "deb":
        # Debian/Ubuntu 仓库可能只提供 InRelease，或同时提供 Release/Packages。
        for suffix in ("/InRelease", "/Release", "/Packages"):
            probe = repo_url.rstrip("/") + suffix
            try:
                text = fetch_text(probe, timeout=20)
            except Exception as exc:
                log("软件源探测失败 %s：%s" % (probe, exc))
                continue
            if ("Origin:" in text or "Suite:" in text or "SHA256:" in text
                    or "Package:" in text):
                return True
        return False
    probe = repo_url.rstrip("/") + "/repodata/repomd.xml"
    try:
        text = fetch_text(probe, timeout=20)
    except Exception as exc:
        log("软件源探测失败 %s：%s" % (probe, exc))
        return False
    return "<repomd" in text


def _choose_repo(params):
    req = params.get("version_req") or {}
    parsed = parse_version_tuple(req.get("version") or params.get("version"))
    if parsed:
        minor = parsed[1]
    else:
        minor = int(req.get("minor") or 0)
    facts = params.get("facts") or detect()
    custom = params.get("repo_base")
    allow_fallback = bool(params.get("allow_upstream_fallback", True))
    try:
        candidates = repository.plan(facts, minor, custom, allow_fallback)
    except ValueError as exc:
        raise ActionFailure("无法生成 Kubernetes 软件源配置：%s" % exc,
                            hint="请确认系统版本、包管理器和 CPU 架构受支持")
    errors = []
    for candidate in candidates:
        log("检测软件源 [%s] %s" % (candidate["source"], candidate["url"]))
        if _repo_available(candidate["url"], candidate["kind"]):
            candidate["fallback"] = candidate["source"] == "upstream"
            log("使用软件源：%s" % repository.describe(candidate))
            if candidate["source"] == "upstream":
                log("当前使用官方 pkgs.k8s.io（非国内镜像），请确认网络合规要求")
            return candidate
        errors.append(candidate["url"])
    minor_text = repository.k8s_minor_text(minor)
    raise ActionFailure(
        "Kubernetes v%s 的软件源不可用" % minor_text,
        errors=errors or ["没有可用的仓库候选"],
        hint="检查节点 DNS/HTTPS 出网和系统 CA；或使用 --package-repo-base 指向企业内网镜像",
    )


def _apt_key(repo_url):
    key_url = repo_url.rstrip("/") + "/Release.key"
    ensure_dir("/usr/share/keyrings", 0o755)
    dest = "/usr/share/keyrings/kube-deploy-kubernetes.gpg"
    tmp = "/var/tmp/kube-deploy-k8s.key"
    download(key_url, tmp, timeout=45, retries=2)
    if shutil.which("gpg"):
        run(["gpg", "--batch", "--yes", "--dearmor", "-o", dest, tmp],
            timeout=60, env=_apt_env())
    else:
        shutil.copy2(tmp, dest)
    os.chmod(dest, 0o644)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return dest


def _configure_apt(repo):
    keyring = _apt_key(repo["url"])
    path = "/etc/apt/sources.list.d/kube-deploy-kubernetes.list"
    backup_file(path)
    write_file(path, repository.apt_source_line(repo, keyring))
    proc = run(_apt_cmd(["update", "-o", "Acquire::Retries=3"]),
               timeout=900, check=False, env=_apt_env())
    if proc.returncode != 0:
        raise ActionFailure("apt-get update 失败",
                            errors=[proc.stderr.strip()[-1000:]],
                            hint="请检查 Ubuntu/Debian 系统软件源和 Kubernetes 仓库的网络连通性")
    return path


def _configure_rpm(repo):
    path = "/etc/yum.repos.d/kubernetes.repo"
    backup_file(path)
    key_url = repo["url"].rstrip("/") + "/repodata/repomd.xml.key"
    # 尝试导入 GPG key；若仓库无 key，包管理器仍会校验包签名并失败。
    tmp = "/tmp/kube-deploy-k8s.repo.key"
    try:
        download(key_url, tmp, timeout=45, retries=2)
        if shutil.which("rpm"):
            run_ok(["rpm", "--import", tmp], timeout=60)
    except Exception as exc:
        log("导入 Kubernetes GPG key 失败：%s" % exc)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    content = repository.rpm_repo_text(repo, key_url)
    write_file(path, content)
    pm = shutil.which("dnf") or shutil.which("yum")
    if not pm:
        raise ActionFailure("未找到 dnf/yum")
    proc = run([pm, "makecache", "--refresh"] if pm.endswith("dnf") else [pm, "makecache", "fast"],
               timeout=900, check=False)
    if proc.returncode != 0:
        raise ActionFailure("软件源元数据刷新失败",
                            errors=[proc.stderr.strip()[-1000:]],
                            hint="请检查系统 yum/dnf 源以及 Kubernetes 仓库网络")
    return path


def _list_apt_candidates(package):
    proc = run_ok(["apt-cache", "madison", package], timeout=60)
    candidates = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0] == package:
            candidates.append(parts[1])
    if not candidates:
        proc = run_ok(["apt-cache", "policy", package], timeout=60)
        for line in (proc.stdout or "").splitlines():
            m = re.search(r"Candidate:\s*(\S+)", line)
            if m and m.group(1) != "(none)":
                candidates.append(m.group(1))
    return candidates


def _list_rpm_candidates(package):
    pm = shutil.which("dnf") or shutil.which("yum")
    if not pm:
        return []
    # 仓库文件里有 exclude=kubelet kubeadm ...；查询候选时必须临时解除，
    # 否则 dnf 会把所有 Kubernetes 包隐藏，表现为“No matching Packages”。
    args = [pm, "--disableexcludes=kubernetes", "--showduplicates",
            "list", package, "--available"]
    proc = run_ok(args, timeout=180)
    if proc.returncode != 0:
        proc = run_ok([pm, "--disableexcludes=kubernetes",
                       "--showduplicates", "list", package], timeout=180)
    candidates = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("available", "installed", "last metadata")):
            continue
        if line.startswith("Available Packages") or line.startswith("Installed Packages"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split(".")[0]
        if name != package:
            continue
        version = parts[1]
        if version and version not in ("", "Package"):
            candidates.append(version)
    return candidates


def _resolve_packages(exact_k8s, pm, candidates_by_pkg, components=None):
    """为指定组件选择与 exact_k8s 相同的包版本。"""
    kv = parse_version_tuple(exact_k8s)
    if not kv:
        raise ActionFailure("内部错误：非法 Kubernetes 版本 %s" % exact_k8s)
    result = {}
    for pkg in (components or ("kubeadm", "kubelet", "kubectl")):
        candidates = candidates_by_pkg.get(pkg, [])
        matched = []
        for raw in candidates:
            m = re.match(r"^(\d+\.\d+\.\d+)", raw)
            if m and parse_version_tuple(m.group(1)) == kv:
                matched.append(raw)
        if not matched:
            raise ActionFailure("软件源中缺少 %s 的 %s 包" % (pkg, exact_k8s),
                                hint="等待镜像同步，或改用 --version 指定已同步版本")
        matched.sort(key=package_version_key, reverse=True)
        result[pkg] = matched[0]
    return result


def _el7_vault_repo():
    osr = read_os_release()
    os_id = (osr.get("ID") or "").lower()
    version = str(osr.get("VERSION_ID") or "")
    arch = os.uname().machine
    if os_id != "centos" or not version.startswith("7"):
        return ""
    if arch == "x86_64":
        base = "https://mirrors.aliyun.com/centos-vault/7.9.2009/os/$basearch/"
    elif arch == "aarch64":
        base = "https://mirrors.aliyun.com/centos-altarch/7.9.2009/os/$basearch/"
    else:
        return ""
    path = "/etc/yum.repos.d/kube-deploy-vault.repo"
    backup_file(path)
    lines = ["[kube-deploy-vault]", "name=CentOS 7 Vault (Aliyun) - KubeDeploy",
             "baseurl=" + base, "enabled=0", "gpgcheck=1",
             "gpgkey=https://mirrors.aliyun.com/centos/RPM-GPG-KEY-CentOS-7"]
    write_file(path, "\n".join(lines) + "\n")
    log("已写入 CentOS 7 vault 备用源（默认禁用），用于安装缺失依赖")
    return path


def _install_base_deps(pm):
    deps_deb = ["conntrack", "socat", "ebtables", "ethtool", "iptables", "iproute2",
                "ipset", "ipvsadm", "nfs-common", "bash-completion",
                "ca-certificates", "curl", "gpg", "tar", "openssl", "libseccomp2"]
    deps_rpm = ["conntrack-tools", "socat", "ebtables", "ethtool", "iptables", "iproute",
                "ipset", "ipvsadm", "nfs-utils", "bash-completion",
                "ca-certificates", "curl", "gnupg2", "tar", "openssl", "libseccomp"]
    if pm == "apt":
        run(_apt_cmd(["update", "-o", "Acquire::Retries=3"]),
            timeout=600, check=False, env=_apt_env())
        proc = run(_apt_cmd(["install", "-y", "--no-install-recommends"] + deps_deb),
                   timeout=900, check=False, env=_apt_env())
    else:
        executable = shutil.which("dnf") or shutil.which("yum")
        proc = run([executable, "install", "-y"] + deps_rpm, timeout=1200, check=False)
    if proc.returncode != 0:
        log("基础依赖安装返回非零，将检查关键命令是否已存在")
    missing = [x for x in ("conntrack", "socat", "iptables", "ethtool", "tar") if not shutil.which(x)]
    repaired = False
    if missing:
        vault = _el7_vault_repo()
        if vault:
            log("尝试使用 CentOS 7 Aliyun vault 源安装缺失依赖")
            exe = shutil.which("yum") or shutil.which("dnf")
            proc = run([exe, "install", "-y", "--disablerepo=*",
                        "--enablerepo=kube-deploy-vault"] + deps_rpm,
                       timeout=1800, check=False)
            missing = [x for x in ("conntrack", "socat", "iptables", "ethtool", "tar")
                       if not shutil.which(x)]
            repaired = proc.returncode == 0 and not missing
    warning = ""
    if missing:
        warning = "缺少关键命令：%s" % ", ".join(missing)
    elif repaired:
        warning = "已自动使用 CentOS 7 Aliyun vault 源补齐依赖"
    return {"ok": not bool(missing), "missing": missing,
            "warning": warning, "el7_vault_repaired": repaired}


def install_packages(params):
    facts = detect()
    pm = facts.get("pkg_mgr")
    if pm not in ("apt", "dnf", "yum"):
        raise ActionFailure("不支持的包管理器：%s" % pm)
    req = params.get("version_req") or {}
    req_obj = VersionRequest(1, int(req.get("minor")), req.get("patch"))
    deps = {"ok": True, "missing": [], "warning": ""}
    if params.get("install_deps", True):
        deps = _install_base_deps(pm)
        if deps["missing"]:
            raise ActionFailure("基础依赖不完整", errors=[deps["warning"]],
                                hint="请为系统配置可用的 OS 软件源，或手动安装缺失命令")
    components = params.get("components") or ["kubelet", "kubeadm", "kubectl"]
    components = [x for x in components if x in ("kubelet", "kubeadm", "kubectl")]
    if not components:
        raise ActionFailure("未指定要安装的组件")
    repo = _choose_repo({"version_req": req, "pkg_mgr": pm, "facts": facts,
                         "repo_base": params.get("repo_base"),
                         "allow_upstream_fallback": params.get("allow_upstream_fallback", True)})
    if pm == "apt":
        _configure_apt(repo)
        candidates = {pkg: _list_apt_candidates(pkg) for pkg in ("kubeadm", "kubelet", "kubectl")}
    else:
        _configure_rpm(repo)
        candidates = {pkg: _list_rpm_candidates(pkg) for pkg in ("kubeadm", "kubelet", "kubectl")}
    if not candidates.get("kubeadm"):
        raise ActionFailure("软件源中未发现 kubeadm 包",
                            errors=["仓库：%s" % repo["url"]])
    exact = (params.get("exact_version") or "").strip()
    if exact:
        exact_k8s = exact if exact.startswith("v") else "v" + exact
        if not parse_version_tuple(exact_k8s):
            raise ActionFailure("非法精确版本：%s" % exact)
    else:
        _, exact_k8s = select_package_version(candidates["kubeadm"], req_obj)
    versions = _resolve_packages(exact_k8s, pm, candidates, components)

    addons = bool(params.get("include_addons", "kubeadm" in components))
    if pm == "apt":
        args = ["install", "-y", "--allow-downgrades"]
        for pkg in components:
            args.append("%s=%s" % (pkg, versions[pkg]))
        if addons:
            args += ["kubernetes-cni", "cri-tools"]
        proc = run(_apt_cmd(args), timeout=1800, check=False, env=_apt_env())
    else:
        executable = shutil.which("dnf") or shutil.which("yum")
        args = [executable, "install", "-y", "--disableexcludes=kubernetes"]
        for pkg in components:
            args.append("%s-%s" % (pkg, versions[pkg]))
        if addons:
            args += ["kubernetes-cni", "cri-tools"]
        proc = run(args, timeout=1800, check=False)
        if proc.returncode != 0:
            log("安装返回非零，尝试 downgrade（兼容降级场景）")
            downgrade_args = [executable, "downgrade", "-y", "--disableexcludes=kubernetes"]
            for pkg in components:
                downgrade_args.append("%s-%s" % (pkg, versions[pkg]))
            proc = run(downgrade_args, timeout=1800, check=False)
    if proc.returncode != 0:
        raise ActionFailure("Kubernetes 软件包安装失败",
                            errors=[proc.stderr.strip()[-1500:]],
                            hint="请检查软件源是否包含目标版本、GPG 校验与磁盘空间")
    if "kubelet" in components:
        run(["systemctl", "enable", "kubelet"], timeout=60, check=False)
    # 校验 kubeadm（如果本次安装/已存在）
    if "kubeadm" in components:
        proc = run(["kubeadm", "version", "-o", "short"], timeout=30, check=False)
        actual = (proc.stdout or "").strip()
        if not actual.startswith(exact_k8s):
            raise ActionFailure("安装后的 kubeadm 版本不符：期望 %s，实际 %s" % (exact_k8s, actual))
    if "kubelet" in components:
        proc = run(["kubelet", "--version"], timeout=30, check=False)
        if exact_k8s.lstrip("v") not in (proc.stdout or ""):
            raise ActionFailure("安装后的 kubelet 版本不符：期望 %s，实际 %s" % (exact_k8s, proc.stdout.strip()))
    cni_dir = "/opt/cni/bin"
    if addons and (not os.path.isdir(cni_dir)
                   or not os.path.exists(os.path.join(cni_dir, "loopback"))
                   or not os.path.exists(os.path.join(cni_dir, "portmap"))):
        raise ActionFailure("Kubernetes CNI 基础插件缺失（/opt/cni/bin）",
                            hint="请确认 kubernetes-cni 包已安装；否则 Flannel 无法创建 Pod 网络")
    return {
        "k8s_version": exact_k8s,
        "packages": versions,
        "components": components,
        "repo_url": repo["url"],
        "repo_source": repo["source"],
        "repo_arch": repo["arch"],
        "repo_minor": repo["k8s_minor"],
        "fallback": repo["fallback"],
        "deps": deps,
        "warnings": ([deps["warning"]] if deps.get("warning") else []),
    }
