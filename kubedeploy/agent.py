"""远程 Python agent 入口。

通过 stdin 接收 JSON：
  {"action": "...", "params": {...}}
stdout 只输出一行 JSON；日志写 stderr。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

from . import __version__
from .agent_base import ActionFailure, Lock, ensure_root, log, redact_text
from . import agent_platform, agent_packages, agent_runtime, agent_ha, agent_cni
from . import agent_kube, agent_state, agent_upgrade, agent_downgrade, agent_package_check


READ_ONLY_ACTIONS = {"detect", "preflight", "cluster_info", "read_state",
                     "ha_status", "cni_status", "upgrade_status"}


def _dispatch(action, params):
    if action == "detect":
        return agent_platform.detect()
    if action == "preflight":
        return agent_platform.preflight(params)
    if action == "prepare":
        ensure_root(); return agent_platform.prepare(params)
    if action == "install_packages":
        ensure_root(); return agent_packages.install_packages(params)
    if action == "check_packages":
        ensure_root(); return agent_package_check.check_packages(params)
    if action == "install_runtime":
        ensure_root(); return agent_runtime.install_runtime(params)
    if action == "set_sandbox_image":
        ensure_root(); return agent_runtime.set_sandbox_image(params)
    if action == "init":
        ensure_root(); return agent_kube.init_cluster(params)
    if action == "join":
        ensure_root(); return agent_kube.join_cluster(params)
    if action == "generate_join":
        ensure_root(); return agent_kube.generate_join(params)
    if action == "preload_pause":
        ensure_root(); return agent_kube.preload_pause(params)
    if action == "wait_ready":
        ensure_root(); return agent_kube.wait_ready(params)
    if action == "untaint_master":
        ensure_root(); return agent_kube.untaint_master(params)
    if action == "wait_coredns":
        ensure_root(); return agent_kube.wait_coredns(params)
    if action == "cluster_info":
        ensure_root(); return agent_kube.cluster_info(params)
    if action == "drain_node":
        ensure_root(); return agent_kube.drain_node(params)
    if action == "uncordon_node":
        ensure_root(); return agent_kube.uncordon_node(params)
    if action == "reset":
        ensure_root(); return agent_kube.reset_node(params)
    if action == "setup_ha":
        ensure_root(); return agent_ha.setup_ha(params)
    if action == "ha_status":
        return agent_ha.ha_status(params)
    if action == "teardown_ha":
        ensure_root(); return agent_ha.teardown_ha(params)
    if action == "preload_cni":
        ensure_root(); return agent_cni.preload_cni(params)
    if action == "deploy_cni":
        ensure_root(); return agent_cni.deploy_cni(params)
    if action == "cni_status":
        return agent_cni.cni_status(params)
    if action == "write_state":
        ensure_root(); return agent_state.write_state(params)
    if action == "read_state":
        return agent_state.read_state(params)
    if action == "backup":
        ensure_root(); return agent_state.create_backup(params)
    if action == "verify_backup":
        ensure_root(); return agent_state.verify_backup(params)
    if action == "upgrade_plan":
        ensure_root(); return agent_upgrade.upgrade_plan(params)
    if action == "upgrade_apply":
        ensure_root(); return agent_upgrade.upgrade_apply(params)
    if action == "upgrade_control_plane_node":
        ensure_root(); return agent_upgrade.upgrade_control_plane_node(params)
    if action == "upgrade_worker_node":
        ensure_root(); return agent_upgrade.upgrade_worker_node(params)
    if action == "kubelet_action":
        ensure_root(); return agent_upgrade.kubelet_action(params)
    if action == "upgrade_status":
        return agent_upgrade.upgrade_status(params)
    if action == "restore_control_plane":
        ensure_root(); return agent_downgrade.restore_control_plane(params)
    if action == "start_node":
        ensure_root(); return agent_downgrade.start_node(params)
    if action == "read_backup_file":
        ensure_root(); return agent_downgrade.read_backup_file(params)
    if action == "write_backup_file":
        ensure_root(); return agent_downgrade.write_backup_file(params)
    raise ActionFailure("未知 agent action：%s" % action)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # sudo 的 secure_path 在 RHEL/CentOS 通常不含 /usr/local/bin，
    # 而 containerd/runc 安装在该目录；显式补全 PATH。
    os.environ["PATH"] = ("/usr/local/sbin:/usr/local/bin:" +
                          os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"))
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    os.environ["NEEDRESTART_MODE"] = "a"
    os.environ["LC_ALL"] = "C"
    request = None
    try:
        if "--request" in argv:
            path = argv[argv.index("--request") + 1]
            with open(path, "r", encoding="utf-8") as fh:
                request = json.load(fh)
        else:
            data = sys.stdin.read()
            if not data.strip():
                raise ActionFailure("agent 未收到请求 JSON")
            request = json.loads(data)
        action = request.get("action")
        params = request.get("params") or {}
        if not action:
            raise ActionFailure("缺少 action")
        log("agent v%s action=%s host=%s" % (__version__, action, os.uname().nodename))
        if action in READ_ONLY_ACTIONS:
            data = _dispatch(action, params)
        else:
            with Lock():
                data = _dispatch(action, params)
        result = {"schema": 1, "ok": True, "action": action, "data": data,
                  "warnings": data.get("warnings", []) if isinstance(data, dict) else []}
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    except ActionFailure as exc:
        result = {"schema": 1, "ok": False, "action": (request or {}).get("action"),
                  "errors": [redact_text(str(x)) for x in exc.errors],
                  "error": redact_text(exc.message), "hint": redact_text(exc.hint)}
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 1
    except Exception as exc:
        log("未处理异常：%s" % exc)
        traceback.print_exc(file=sys.stderr)
        result = {"schema": 1, "ok": False, "action": (request or {}).get("action"),
                  "errors": [str(exc)], "error": "agent 内部错误"}
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
