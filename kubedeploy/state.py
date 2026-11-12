"""本地集群状态/清单持久化。"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from .errors import StateError
from .util import atomic_write, ensure_dir, read_json, safe_slug, utc_now


def state_home():
    return os.environ.get("KUBEDEPLOY_HOME") or os.path.join(
        os.path.expanduser("~"), ".kube", "kube-deploy")


def cluster_dir():
    path = os.path.join(state_home(), "clusters")
    ensure_dir(path, 0o700)
    return path


def kubeconfig_dir():
    path = os.path.join(state_home(), "kubeconfigs")
    ensure_dir(path, 0o700)
    return path


def default_kubeconfig_path():
    """kubectl 默认读取的 kubeconfig；测试可用环境变量覆盖。"""
    return os.environ.get("KUBEDEPLOY_DEFAULT_KUBECONFIG") or os.path.join(
        os.path.expanduser("~"), ".kube", "config")


def _kubeconfig_server(text):
    m = re.search(r"(?m)^\s*server:\s*(\S+)\s*$", text or "")
    return m.group(1) if m else ""


def _write_private(path, content):
    tmp = path + ".kubedeploy-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def install_default_kubeconfig(content):
    """写入/更新默认 kubeconfig，使 kubectl 开箱可用。

    - 不存在：直接写入；
    - 已存在且 server 相同（例如重建了同地址集群）：备份后替换；
    - 已存在且 server 不同：不覆盖，避免破坏其他集群。
    """
    path = default_kubeconfig_path()
    ensure_dir(os.path.dirname(path), 0o700)
    if os.path.lexists(path):
        if os.path.islink(path):
            return path, False
        try:
            if os.path.getsize(path) == 0:
                os.unlink(path)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    old = fh.read()
                old_server = _kubeconfig_server(old)
                new_server = _kubeconfig_server(content)
                if not old_server or old_server != new_server:
                    return path, False
                backup = "%s.kubedeploy-%s.bak" % (path, time.strftime("%Y%m%d-%H%M%S"))
                try:
                    with open(backup, "w", encoding="utf-8") as fh:
                        fh.write(old)
                    os.chmod(backup, 0o600)
                except OSError:
                    pass
                _write_private(path, content)
                return path, True
        except OSError:
            return path, False
    _write_private(path, content)
    return path, True


@dataclass
class BackupRecord:
    backup_id: str
    from_version: str
    to_version: str
    created_at: str
    etcd_snapshot: bool = False
    control_plane_archive: bool = False
    nodes: List[dict] = field(default_factory=list)
    note: str = ""


@dataclass
class ClusterRecord:
    name: str
    version: str
    endpoint: str
    spec: dict
    node_count: int = 0
    master_count: int = 0
    worker_count: int = 0
    facts: dict = field(default_factory=dict)
    backups: List[dict] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def save(self):
        path = os.path.join(cluster_dir(), "%s.json" % safe_slug(self.name))
        data = asdict(self)
        data["updated_at"] = utc_now()
        self.updated_at = data["updated_at"]
        atomic_write(path, read_json_text(data), 0o600)
        return path

    def add_history(self, action, detail=""):
        self.history.append({"time": utc_now(), "action": action, "detail": detail})

    def add_backup(self, record):
        self.backups.append(asdict(record) if hasattr(record, "__dataclass_fields__") else record)

    def kubeconfig_path(self):
        # 只计算路径，不在打印/状态查询时触发目录创建（避免只读 HOME 崩溃）
        return os.path.join(state_home(), "kubeconfigs", "%s.conf" % safe_slug(self.name))

    def save_kubeconfig(self, content):
        path = self.kubeconfig_path()
        ensure_dir(os.path.dirname(path), 0o700)
        atomic_write(path, content, 0o600)
        return path

    @classmethod
    def load(cls, name):
        path = os.path.join(cluster_dir(), "%s.json" % safe_slug(name))
        try:
            data = read_json(path)
        except FileNotFoundError:
            raise StateError("找不到集群状态：%s" % name,
                             hint="状态文件位于 %s；可以用 --name 指定集群名" % path)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def list_all(cls):
        result = []
        for name in sorted(os.listdir(cluster_dir())):
            if not name.endswith(".json"):
                continue
            try:
                data = read_json(os.path.join(cluster_dir(), name))
                result.append(cls(**{k: v for k, v in data.items()
                                     if k in cls.__dataclass_fields__}))
            except Exception:
                continue
        return result


def read_json_text(data):
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)


def new_backup_name(current, target):
    return "upgrade-%s-to-%s-%s" % (
        current.lstrip("v").replace(".", "_"),
        target.lstrip("v").replace(".", "_"),
        time.strftime("%Y%m%d-%H%M%S"))
