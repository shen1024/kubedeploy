"""节点状态持久化与升级前备份（etcd + 控制平面配置）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time

from .agent_base import (ActionFailure, ensure_dir, ensure_root, log, read_file,
                         run, run_ok, sha256_file, write_file)
from .constants import AGENT_BACKUP_DIR, AGENT_STATE_DIR, AGENT_STATE_FILE
from .util import atomic_write, b64e, read_json, utc_now


def write_state(params):
    ensure_root()
    ensure_dir(AGENT_STATE_DIR, 0o700)
    state = params.get("state")
    if not isinstance(state, dict):
        raise ActionFailure("state 必须是 JSON 对象")
    atomic_write(AGENT_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2), 0o600)
    return {"path": AGENT_STATE_FILE, "updated_at": utc_now()}


def read_state(params):
    if not os.path.exists(AGENT_STATE_FILE):
        return {"exists": False}
    try:
        return {"exists": True, "state": read_json(AGENT_STATE_FILE)}
    except Exception as exc:
        raise ActionFailure("读取节点状态失败：%s" % exc)


def _kubectl_json(args, timeout=60):
    proc = run(["kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf"] + args,
               timeout=timeout, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubectl 查询失败", errors=[proc.stderr.strip()[-800:]])
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise ActionFailure("kubectl 返回非 JSON")


def _find_etcd_pod(node_name):
    data = _kubectl_json(["-n", "kube-system", "get", "pods", "-l", "component=etcd",
                          "-o", "json"])
    for item in data.get("items", []):
        if item.get("spec", {}).get("nodeName") == node_name:
            return item.get("metadata", {}).get("name")
    # 某些发行版 label 不同，按名称 fallback
    data = _kubectl_json(["-n", "kube-system", "get", "pods", "-o", "json"])
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if name.startswith("etcd-") and item.get("spec", {}).get("nodeName") == node_name:
            return name
    return ""


def create_backup(params):
    ensure_root()
    backup_id = params.get("backup_id") or time.strftime("upgrade-%Y%m%d-%H%M%S")
    if not re.match(r"^[A-Za-z0-9._-]+$", backup_id):
        raise ActionFailure("非法 backup_id")
    node_name = params.get("node_name") or socket.gethostname()
    if not os.path.exists("/etc/kubernetes/admin.conf"):
        raise ActionFailure("当前节点不是控制平面节点，无法创建控制平面备份")
    backup_dir = os.path.join(AGENT_BACKUP_DIR, backup_id, node_name)
    ensure_dir(backup_dir, 0o700)
    etcd_pod = _find_etcd_pod(node_name)
    if not etcd_pod:
        raise ActionFailure("未找到本节点的 etcd static pod",
                            hint="当前集群可能使用外部 etcd；本工具的自动备份/回滚需要堆叠 etcd")
    log("备份 etcd：pod=%s" % etcd_pod)
    snapshot_name = "snapshot-%s.db" % re.sub(r"[^A-Za-z0-9._-]", "_", node_name)
    remote_snapshot = "/var/lib/etcd/" + snapshot_name
    try:
        os.unlink(remote_snapshot)
    except OSError:
        pass
    cmd = ["kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
           "-n", "kube-system", "exec", etcd_pod, "--",
           "etcdctl", "--endpoints=https://127.0.0.1:2379",
           "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
           "--cert=/etc/kubernetes/pki/etcd/server.crt",
           "--key=/etc/kubernetes/pki/etcd/server.key",
           "snapshot", "save", remote_snapshot]
    proc = run(cmd, timeout=900, check=False)
    if proc.returncode != 0:
        raise ActionFailure("etcd 快照失败", errors=[proc.stderr.strip()[-1200:]],
                            hint="确认 etcd 容器内存在 etcdctl，且 /var/lib/etcd 为 hostPath 挂载")
    host_snapshot = remote_snapshot
    if not os.path.exists(host_snapshot):
        raise ActionFailure("etcd 快照文件未出现在宿主机：%s" % host_snapshot)
    snapshot_path = os.path.join(backup_dir, "etcd-snapshot.db")
    shutil.copy2(host_snapshot, snapshot_path)
    os.chmod(snapshot_path, 0o600)
    try:
        os.unlink(host_snapshot)
    except OSError:
        pass
    if os.path.getsize(snapshot_path) < 1024:
        raise ActionFailure("etcd 快照文件异常小：%s" % snapshot_path)

    archive_path = os.path.join(backup_dir, "control-plane.tgz")
    paths = ["etc/kubernetes", "etc/cni", "etc/containerd/config.toml",
             "etc/systemd/system/kubelet.service.d", "var/lib/kubelet/config.yaml",
             "var/lib/kubelet/kubeadm-flags.env", "etc/default/kubelet", "etc/sysconfig/kubelet"]
    existing = [p for p in paths if os.path.exists("/" + p)]
    cmd = ["tar", "-czf", archive_path, "-C", "/"] + existing
    proc = run(cmd, timeout=300, check=False)
    if proc.returncode != 0:
        raise ActionFailure("控制平面配置归档失败", errors=[proc.stderr.strip()[-800:]])
    os.chmod(archive_path, 0o600)

    etcd_manifest = read_file("/etc/kubernetes/manifests/etcd.yaml")
    metadata = {
        "backup_id": backup_id,
        "node_name": node_name,
        "created_at": utc_now(),
        "current_version": params.get("current_version") or "",
        "target_version": params.get("target_version") or "",
        "etcd_pod": etcd_pod,
        "snapshot_path": snapshot_path,
        "snapshot_sha256": sha256_file(snapshot_path),
        "snapshot_size": os.path.getsize(snapshot_path),
        "archive_path": archive_path,
        "archive_sha256": sha256_file(archive_path),
        "etcd_manifest": etcd_manifest,
        "hostname": socket.gethostname(),
        "node_ip": params.get("node_ip") or "",
    }
    write_file(os.path.join(backup_dir, "metadata.json"),
               json.dumps(metadata, ensure_ascii=False, indent=2), 0o600)
    return metadata


def verify_backup(params):
    """校验本节点上的备份元数据、快照和控制平面归档是否完整。"""
    ensure_root()
    backup_id = params.get("backup_id")
    node_name = params.get("node_name") or socket.gethostname()
    if not backup_id:
        raise ActionFailure("缺少 backup_id")
    meta_path = os.path.join(AGENT_BACKUP_DIR, backup_id, node_name, "metadata.json")
    if not os.path.exists(meta_path):
        raise ActionFailure("本节点缺少备份元数据：%s" % meta_path)
    try:
        metadata = read_json(meta_path)
    except Exception as exc:
        raise ActionFailure("备份元数据无法解析：%s" % exc)
    checks = []
    for key, sha_key in (("snapshot_path", "snapshot_sha256"),
                         ("archive_path", "archive_sha256")):
        if key == "snapshot_path" and params.get("skip_snapshot"):
            continue
        path = metadata.get(key)
        if not path or not os.path.exists(path):
            raise ActionFailure("备份文件不存在：%s" % path)
        size = os.path.getsize(path)
        if size < 1024:
            raise ActionFailure("备份文件异常小：%s" % path)
        if metadata.get(sha_key) and sha256_file(path) != metadata[sha_key]:
            raise ActionFailure("备份文件 SHA256 不匹配：%s" % path)
        checks.append({"file": path, "size": size})
    return {"ok": True, "backup_id": backup_id, "node": node_name, "files": checks}
