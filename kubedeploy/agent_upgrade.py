"""控制平面/节点滚动升级。"""

from __future__ import annotations

import os
import re

from .agent_base import ActionFailure, ensure_root, log, run, run_ok, service_active
from .agent_kube import _kubectl, _prepare_images, set_cluster_image_repository
from .agent_packages import install_packages
from .util import parse_version_tuple


def _install_target(params, components):
    p = {
        "version_req": params.get("version_req") or {
            "minor": parse_version_tuple(params["target"])[1],
            "patch": parse_version_tuple(params["target"])[2],
        },
        "exact_version": params["target"],
        "components": components,
        "repo_base": params.get("repo_base"),
        "allow_upstream_fallback": params.get("allow_upstream_fallback", True),
        "install_deps": params.get("install_deps", False),
        "include_addons": bool(params.get("include_addons", False)),
    }
    return install_packages(p)


def upgrade_plan(params):
    ensure_root()
    target = params.get("target")
    if not target:
        raise ActionFailure("缺少升级目标版本")
    proc = run(["kubeadm", "upgrade", "plan", target], timeout=600, check=False)
    # 计划失败通常代表跨版本或镜像不可达，直接返回供控制端判断
    return {"target": target, "ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr)[-5000:]}


def upgrade_apply(params):
    ensure_root()
    target = params.get("target")
    if not target:
        raise ActionFailure("缺少升级目标版本")
    # 先用当前 kubeadm 预拉取目标镜像/验证 sandbox 配置；失败时不要先把
    # kubeadm 升级到目标版本，避免留下半升级状态。
    image = _prepare_images({
        "version": target,
        "image_repositories": params.get("image_repositories"),
        "allow_upstream_fallback": params.get("allow_upstream_fallback", True),
    })
    installed = _install_target(params, ["kubeadm"])
    set_cluster_image_repository(image["repository"])
    cmd = ["kubeadm", "upgrade", "apply", "-y", target]
    proc = run(cmd, timeout=2400, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubeadm upgrade apply 失败",
                            errors=[proc.stderr.strip()[-1800:]],
                            hint="升级前请确认已创建备份；修复后可回滚到备份")
    installed_kubelet = _install_target(params, ["kubelet", "kubectl"])
    _restart_kubelet()
    return {"target": target, "kubeadm": installed["packages"].get("kubeadm", ""),
            "kubelet": installed_kubelet["packages"].get("kubelet", ""),
            "output": (proc.stdout + proc.stderr)[-5000:]}


def upgrade_control_plane_node(params):
    ensure_root()
    target = params.get("target")
    if not target:
        raise ActionFailure("缺少升级目标版本")
    _install_target(params, ["kubeadm"])
    proc = run(["kubeadm", "upgrade", "node"],
               timeout=1800, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubeadm upgrade node 失败（控制平面）",
                            errors=[proc.stderr.strip()[-1600:]])
    _install_target(params, ["kubelet", "kubectl"])
    _restart_kubelet()
    return {"target": target, "output": (proc.stdout + proc.stderr)[-4000:]}


def upgrade_worker_node(params):
    ensure_root()
    target = params.get("target")
    if not target:
        raise ActionFailure("缺少升级目标版本")
    # worker 也需要 kubeadm 来执行 node 阶段，保持版本一致
    _install_target(params, ["kubeadm"])
    proc = run(["kubeadm", "upgrade", "node"],
               timeout=1200, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubeadm upgrade node 失败（worker）",
                            errors=[proc.stderr.strip()[-1600:]])
    _install_target(params, ["kubelet", "kubectl"])
    _restart_kubelet()
    return {"target": target}


def _restart_kubelet():
    run(["systemctl", "daemon-reload"], timeout=60, check=False)
    run(["systemctl", "restart", "kubelet"], timeout=180, check=False)
    if not service_active("kubelet"):
        raise ActionFailure("kubelet 重启失败",
                            hint="查看 journalctl -u kubelet -n 100")


def kubelet_action(params):
    ensure_root()
    action = params.get("operation") or "status"
    if action == "stop":
        run(["systemctl", "stop", "kubelet"], timeout=180, check=False)
    elif action == "start":
        run(["systemctl", "start", "kubelet"], timeout=180, check=False)
    elif action == "restart":
        _restart_kubelet()
    proc = run_ok(["systemctl", "is-active", "kubelet"], timeout=20)
    version = run_ok(["kubelet", "--version"], timeout=20).stdout.strip()
    return {"active": (proc.stdout or "").strip() == "active",
            "version": version, "operation": action}


def upgrade_status(params):
    proc = run_ok(["kubeadm", "version", "-o", "short"], timeout=20)
    kubelet = run_ok(["kubelet", "--version"], timeout=20)
    return {"kubeadm": (proc.stdout or "").strip(),
            "kubelet": (kubelet.stdout or "").strip(),
            "kubelet_active": service_active("kubelet")}
