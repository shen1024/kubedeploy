"""只读式软件包可用性检查（会配置仓库元数据，不安装软件包）。"""

from __future__ import annotations

import shutil

from .agent_base import ActionFailure, run
from .agent_packages import (_choose_repo, _configure_apt, _configure_rpm,
                             _list_apt_candidates, _list_rpm_candidates,
                             _resolve_packages)
from .agent_platform import detect
from .util import parse_version_tuple
from .versions import VersionRequest, select_package_version


def check_packages(params):
    facts = detect()
    pm = facts.get("pkg_mgr")
    if pm not in ("apt", "dnf", "yum"):
        raise ActionFailure("不支持的包管理器：%s" % pm)
    req = params.get("version_req") or {}
    req_obj = VersionRequest(1, int(req.get("minor")), req.get("patch"))
    repo = _choose_repo({"version_req": req, "pkg_mgr": pm,
                         "repo_base": params.get("repo_base"),
                         "allow_upstream_fallback": params.get("allow_upstream_fallback", True)})
    if pm == "apt":
        _configure_apt(repo)
        candidates = {pkg: _list_apt_candidates(pkg) for pkg in ("kubeadm", "kubelet", "kubectl")}
    else:
        _configure_rpm(repo)
        candidates = {pkg: _list_rpm_candidates(pkg) for pkg in ("kubeadm", "kubelet", "kubectl")}
    exact = (params.get("exact_version") or "").strip()
    if exact:
        exact_k8s = exact if exact.startswith("v") else "v" + exact
    else:
        _, exact_k8s = select_package_version(candidates["kubeadm"], req_obj)
    versions = _resolve_packages(exact_k8s, pm, candidates,
                                 params.get("components") or ["kubelet", "kubeadm", "kubectl"])
    return {"k8s_version": exact_k8s, "packages": versions,
            "repo_url": repo["url"], "repo_source": repo["source"],
            "fallback": repo["fallback"]}
