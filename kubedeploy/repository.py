"""Kubernetes 软件源规划：按 K8s minor、包管理器、CPU 架构生成仓库配置。

该模块不访问网络，便于单元测试；网络探测在 agent_packages 中完成。
"""

from __future__ import annotations

import re

from .constants import (DEFAULT_KUBE_REPO_BASE, K8S_MAX_MINOR, K8S_MIN_MINOR,
                        UPSTREAM_KUBE_REPO_BASE)


def k8s_minor_text(minor):
    """把 34 转成 Kubernetes 仓库使用的 1.34。"""
    minor = int(minor)
    if minor < K8S_MIN_MINOR or minor > K8S_MAX_MINOR:
        raise ValueError("Kubernetes minor 超出范围：%s" % minor)
    return "1.%d" % minor


def repo_kind(pkg_mgr):
    if pkg_mgr == "apt":
        return "deb"
    if pkg_mgr in ("dnf", "yum"):
        return "rpm"
    raise ValueError("不支持的包管理器：%s" % pkg_mgr)


def apt_arch(arch):
    mapping = {"amd64": "amd64", "x86_64": "amd64",
               "arm64": "arm64", "aarch64": "arm64"}
    value = mapping.get((arch or "").lower())
    if not value:
        raise ValueError("不支持的 CPU 架构：%s" % arch)
    return value


def _replacements(minor, arch, pkg_mgr, facts):
    return {
        "minor": k8s_minor_text(minor),
        "minor_num": str(int(minor)),
        "arch": arch,
        "pkg_mgr": pkg_mgr,
        "os_id": (facts or {}).get("os_id", ""),
        "os_version": (facts or {}).get("os_version", ""),
        "os_major": str((facts or {}).get("os_version", "")).split(".")[0],
        "arch_raw": (facts or {}).get("arch_raw", arch),
    }


def format_base(base, minor, arch="amd64", pkg_mgr="dnf", facts=None):
    """格式化仓库根，并在末尾补齐 /v1.34。

    自定义模板支持 {minor}/{minor_num}/{arch}/{pkg_mgr}/{os_id}/{os_version}/
    {os_major}/{arch_raw}。例如：
      https://mirror.example.com/k8s/{os_id}/{os_version}/{arch}/v{minor}
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ValueError("软件源地址不能为空")
    # 允许用户直接粘贴以 /deb 或 /rpm 结尾的完整仓库地址；plan 会再补类型。
    base = re.sub(r"/(?:deb|rpm)$", "", base)
    repl = _replacements(minor, arch, pkg_mgr, facts)
    if "{" in base:
        try:
            base = base.format(**repl)
        except KeyError as exc:
            raise ValueError("软件源模板包含未知占位符：%s" % exc)
    # 已包含 v1.34 时不重复追加；允许自定义路径以 /v1.34 结尾。
    if re.search(r"(?:^|/)v1\.\d+(?:/|$)", base):
        return base
    return base + "/v" + repl["minor"]


def plan(facts, minor, custom_base=None, allow_upstream_fallback=True):
    """返回按优先级排列的仓库候选。

    facts 使用 detect() 的结果；只取 pkg_mgr/arch/os_id/os_version/arch_raw。
    """
    pkg_mgr = (facts or {}).get("pkg_mgr") or ""
    kind = repo_kind(pkg_mgr)
    arch = apt_arch((facts or {}).get("arch") or "")
    items = []
    if custom_base:
        items.append(("custom", format_base(custom_base, minor, arch, pkg_mgr, facts)))
    items.append(("aliyun", format_base(DEFAULT_KUBE_REPO_BASE, minor, arch, pkg_mgr, facts)))
    if allow_upstream_fallback:
        items.append(("upstream", format_base(UPSTREAM_KUBE_REPO_BASE, minor, arch, pkg_mgr, facts)))
    # 去重但保留优先级
    seen = set()
    result = []
    for source, base in items:
        if base in seen:
            continue
        seen.add(base)
        result.append({
            "source": source,
            "base": base,
            "url": base.rstrip("/") + "/" + kind,
            "kind": kind,
            "pkg_mgr": pkg_mgr,
            "arch": arch,
            "k8s_minor": k8s_minor_text(minor),
            "os_id": (facts or {}).get("os_id", ""),
            "os_version": (facts or {}).get("os_version", ""),
            "os_name": (facts or {}).get("os_name", ""),
        })
    return result


def apt_source_line(repo, keyring):
    if repo["kind"] != "deb":
        raise ValueError("不是 deb 仓库")
    return "deb [arch=%s signed-by=%s] %s /\n" % (
        repo["arch"], keyring, repo["url"])


def rpm_repo_text(repo, key_url):
    if repo["kind"] != "rpm":
        raise ValueError("不是 rpm 仓库")
    return """[kubernetes]
name=Kubernetes %s (%s)
baseurl=%s
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=%s
sslverify=1
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
""" % (repo["k8s_minor"], repo["source"], repo["url"], key_url)


def describe(repo):
    return "%s / %s / %s / Kubernetes v%s / %s" % (
        repo.get("os_name") or repo.get("os_id") or "Linux",
        repo.get("os_version") or "-",
        repo.get("arch") or "-",
        repo.get("k8s_minor") or "-",
        repo.get("url") or "-")
