"""基于升级前 etcd 快照的降级/回滚。

Kubernetes 不支持原地降级。本模块恢复升级前的 etcd 快照和控制平面配置，
因此会丢失升级后写入的数据；调用方必须明确确认。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import time

from .agent_base import (ActionFailure, backup_file, ensure_dir, ensure_root, log,
                         read_file, run, run_ok, service_active, sha256_file, write_file)
from .constants import AGENT_BACKUP_DIR
from .util import b64e, utc_now


def _metadata(backup_id, node_name):
    path = os.path.join(AGENT_BACKUP_DIR, backup_id, node_name, "metadata.json")
    if not os.path.exists(path):
        raise ActionFailure("本节点没有该备份：%s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh), path


def _etcd_args(manifest):
    def find_arg(name):
        m = re.search(r"--%s(?:=|\s+)(\S+)" % re.escape(name), manifest)
        return m.group(1).strip("'\"") if m else ""
    name = find_arg("name")
    peer = find_arg("initial-advertise-peer-urls")
    cluster = find_arg("initial-cluster")
    token = find_arg("initial-cluster-token") or "etcd-cluster"
    data_dir = find_arg("data-dir") or "/var/lib/etcd"
    image = ""
    m = re.search(r"(?m)^\s*(?:-\s*)?image:\s*(\S+)\s*$", manifest)
    if m:
        image = m.group(1).strip("'\"")
    return {"name": name, "peer": peer, "cluster": cluster, "token": token,
            "data_dir": data_dir, "image": image}


def _stop_etcd():
    proc = run_ok(["crictl", "ps", "--name", "etcd", "-q"], timeout=60)
    ids = [x.strip() for x in (proc.stdout or "").splitlines() if x.strip()]
    for cid in ids:
        log("停止 etcd 容器 %s" % cid)
        run_ok(["crictl", "stop", "-t", "10", cid], timeout=60)
    time.sleep(2)
    if run_ok(["pgrep", "-x", "etcd"], timeout=10).stdout.strip():
        log("etcd 进程仍在运行，发送 TERM")
        run_ok(["pkill", "-TERM", "-x", "etcd"], timeout=20)
        time.sleep(5)
        if run_ok(["pgrep", "-x", "etcd"], timeout=10).stdout.strip():
            run_ok(["pkill", "-KILL", "-x", "etcd"], timeout=20)
            time.sleep(2)


def _ensure_image(image):
    if not image:
        raise ActionFailure("无法从备份元数据确定 etcd 镜像")
    proc = run_ok(["ctr", "-n", "k8s.io", "images", "ls", "-q"], timeout=60)
    if image not in (proc.stdout or "").splitlines():
        log("拉取 etcd 镜像 %s" % image)
        proc = run(["ctr", "-n", "k8s.io", "images", "pull", image],
                   timeout=600, check=False)
        if proc.returncode != 0:
            raise ActionFailure("无法拉取 etcd 镜像 %s" % image,
                                errors=[proc.stderr.strip()[-800:]])


def _restore_etcd(metadata, snapshot_path, node_name):
    args = _etcd_args(metadata.get("etcd_manifest") or "")
    if not args["name"] or not args["peer"] or not args["cluster"]:
        raise ActionFailure("备份中的 etcd 静态 Pod 参数不完整，无法安全恢复",
                            hint="请手工使用 etcdctl snapshot restore 恢复")
    data_dir = args["data_dir"]
    if not data_dir.startswith("/var/lib/etcd"):
        raise ActionFailure("etcd data-dir 不在预期路径：%s" % data_dir)
    _ensure_image(args["image"])
    snap_dir = os.path.dirname(snapshot_path)
    snap_name = os.path.basename(snapshot_path)
    ensure_dir(data_dir, 0o700)
    # 清理旧数据
    for entry in os.listdir(data_dir):
        path = os.path.join(data_dir, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    # 清理可能存在的恢复容器
    run_ok(["ctr", "-n", "k8s.io", "task", "delete", "-f", "kube-deploy-restore"], timeout=30)
    run_ok(["ctr", "-n", "k8s.io", "container", "delete", "kube-deploy-restore"], timeout=30)
    cmd = ["ctr", "-n", "k8s.io", "run", "--rm", "--user", "0:0",
           "--mount", "type=bind,src=%s,dst=/backup,options=rbind:ro" % snap_dir,
           "--mount", "type=bind,src=%s,dst=%s,options=rbind:rw" % (data_dir, data_dir),
           args["image"], "kube-deploy-restore",
           "/usr/local/bin/etcdctl", "snapshot", "restore", "/backup/" + snap_name,
           "--name", args["name"],
           "--data-dir", data_dir,
           "--initial-cluster", args["cluster"],
           "--initial-cluster-token", args["token"],
           "--initial-advertise-peer-urls", args["peer"]]
    proc = run(cmd, timeout=1200, check=False)
    if proc.returncode != 0:
        raise ActionFailure("etcd 快照恢复失败",
                            errors=[proc.stderr.strip()[-1600:]],
                            hint="检查 etcd 镜像是否包含 /usr/local/bin/etcdctl、快照完整性和磁盘空间")
    return {"name": args["name"], "peer": args["peer"], "data_dir": data_dir}


def _stop_kubelet():
    run(["systemctl", "stop", "kubelet"], timeout=180, check=False)
    deadline = time.time() + 60
    while time.time() < deadline:
        if not service_active("kubelet"):
            break
        time.sleep(2)


def _start_kubelet():
    run(["systemctl", "daemon-reload"], timeout=60, check=False)
    run(["systemctl", "start", "kubelet"], timeout=180, check=False)


def _restore_archive(metadata):
    path = metadata.get("archive_path")
    if not path or not os.path.exists(path):
        raise ActionFailure("控制平面备份归档不存在：%s" % path)
    if metadata.get("archive_sha256") and sha256_file(path) != metadata["archive_sha256"]:
        raise ActionFailure("控制平面备份归档 SHA256 不匹配")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists("/etc/kubernetes"):
        shutil.move("/etc/kubernetes", "/etc/kubernetes.pre-downgrade-" + stamp)
    proc = run(["tar", "-xzf", path, "-C", "/"], timeout=300, check=False)
    if proc.returncode != 0:
        raise ActionFailure("恢复控制平面配置失败", errors=[proc.stderr.strip()[-1000:]])
    return path


def restore_control_plane(params):
    ensure_root()
    backup_id = params.get("backup_id")
    node_name = params.get("node_name") or socket.gethostname()
    snapshot_path = params.get("snapshot_path")
    if not backup_id or not snapshot_path:
        raise ActionFailure("缺少 backup_id 或 snapshot_path")
    if not os.path.exists(snapshot_path):
        raise ActionFailure("快照文件不存在：%s" % snapshot_path)
    if params.get("snapshot_sha256") and sha256_file(snapshot_path) != params["snapshot_sha256"]:
        raise ActionFailure("快照 SHA256 校验失败")
    metadata, _ = _metadata(backup_id, node_name)
    log("停止 kubelet 并恢复控制平面配置")
    _stop_kubelet()
    _stop_etcd()
    if params.get("restore_archive", True):
        _restore_archive(metadata)
    result = _restore_etcd(metadata, snapshot_path, node_name)
    result.update({"backup_id": backup_id, "node": node_name,
                   "restored_at": utc_now(), "archive": metadata.get("archive_path")})
    if params.get("start", False):
        _start_kubelet()
        result["started"] = True
    return result


def start_node(params):
    ensure_root()
    _start_kubelet()
    return {"started": True, "kubelet_active": service_active("kubelet")}


def read_backup_file(params):
    path = params.get("snapshot_path")
    if not path or not os.path.exists(path):
        raise ActionFailure("快照文件不存在")
    size = os.path.getsize(path)
    limit = int(params.get("max_size") or 512 * 1024 * 1024)
    if size > limit:
        raise ActionFailure("快照过大（%.1f MB），超过单次传输上限" % (size / 1024.0 / 1024.0),
                            hint="请手工复制 etcd 快照到其他控制平面节点")
    with open(path, "rb") as fh:
        data = fh.read()
    return {"snapshot_b64": b64e(data), "size": size, "sha256": sha256_file(path),
            "path": path}


def write_backup_file(params):
    ensure_root()
    backup_id = params.get("backup_id")
    node_name = params.get("node_name") or socket.gethostname()
    if not backup_id or not re.match(r"^[A-Za-z0-9._-]+$", backup_id):
        raise ActionFailure("非法 backup_id")
    dest_dir = os.path.join(AGENT_BACKUP_DIR, backup_id, node_name)
    ensure_dir(dest_dir, 0o700)
    dest = params.get("dest_path") or os.path.join(dest_dir, "etcd-snapshot-restore.db")
    if not os.path.abspath(dest).startswith(os.path.abspath(AGENT_BACKUP_DIR) + os.sep):
        raise ActionFailure("非法的备份写入路径")
    data_b64 = params.get("data_b64") or ""
    try:
        data = base64.b64decode(data_b64.encode("ascii"), validate=True)
    except Exception:
        raise ActionFailure("备份数据不是合法 base64")
    if params.get("sha256") and __import__("hashlib").sha256(data).hexdigest() != params["sha256"]:
        raise ActionFailure("传输后的快照 SHA256 不匹配")
    write_file(dest, data, 0o600)
    return {"path": dest, "size": len(data), "sha256": sha256_file(dest)}
