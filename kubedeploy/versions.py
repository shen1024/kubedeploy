"""Kubernetes 版本解析、支持范围与升级路径。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .constants import K8S_MINORS, K8S_MIN_MINOR, K8S_MAX_MINOR
from .errors import ValidationError
from .util import compare_versions, parse_version_tuple, package_version_key


_REQ_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$")


@dataclass(frozen=True)
class VersionRequest:
    major: int
    minor: int
    patch: Optional[int] = None

    @property
    def minor_text(self):
        return "1.%d" % self.minor

    @property
    def is_minor_only(self):
        return self.patch is None

    @property
    def text(self):
        if self.patch is None:
            return self.minor_text
        return "1.%d.%d" % (self.minor, self.patch)

    @property
    def kubeadm_version(self):
        """包管理器安装阶段使用 minor；解析后再改为精确版本。"""
        if self.patch is None:
            return "v%s" % self.minor_text
        return "v%s" % self.text


def parse_version_request(value, allow_latest=True):
    value = (value or "").strip()
    if not value:
        raise ValidationError("必须指定 Kubernetes 版本")
    if value.lower() in ("latest", "stable"):
        if not allow_latest:
            raise ValidationError("此处必须指定精确版本")
        return VersionRequest(1, K8S_MAX_MINOR)
    m = _REQ_RE.match(value)
    if not m:
        raise ValidationError("版本格式错误：%s（示例：1.34、v1.34.6）" % value)
    major = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch")) if m.group("patch") is not None else None
    if major != 1 or minor not in K8S_MINORS:
        raise ValidationError(
            "仅支持 Kubernetes 1.%d - 1.%d，收到 1.%d" % (K8S_MIN_MINOR, K8S_MAX_MINOR, minor),
            hint="本工具支持的版本范围是 v1.28.x 至 v1.37.x",
        )
    if patch is not None and (patch < 0 or patch > 99):
        raise ValidationError("非法补丁版本：%s" % value)
    return VersionRequest(major, minor, patch)


def normalize_version(value):
    """返回 v1.34.2 形式；允许 minor-only。"""
    req = parse_version_request(value)
    return "v" + req.text


def version_tuple(value):
    return parse_version_tuple(value)


def version_minor(value):
    parsed = parse_version_tuple(value)
    if not parsed:
        raise ValidationError("非法 Kubernetes 版本：%s" % value)
    return parsed[1]


def is_version_less(a, b):
    return compare_versions(a, b) < 0


def validate_upgrade_path(current, target, allow_multi_minor=False):
    """校验升级路径；kubeadm 官方一次只支持一个次版本。"""
    cur = parse_version_tuple(current)
    tgt = parse_version_tuple(target)
    if not cur or not tgt:
        raise ValidationError("升级版本格式错误：%s -> %s" % (current, target))
    if cur >= tgt:
        raise ValidationError("目标版本 %s 必须高于当前版本 %s" % (target, current))
    step = tgt[1] - cur[1]
    if step > 1 and not allow_multi_minor:
        raise ValidationError(
            "Kubernetes 不支持跨多个次版本直接升级：%s -> %s" % (current, target),
            hint="请逐个次版本升级；每次升级后验证业务，再执行下一次",
        )
    return step


def validate_downgrade_target(current, target):
    cur = parse_version_tuple(current)
    tgt = parse_version_tuple(target)
    if not cur or not tgt:
        raise ValidationError("降级版本格式错误：%s -> %s" % (current, target))
    if tgt >= cur:
        raise ValidationError("降级目标 %s 必须低于当前版本 %s" % (target, current))
    return cur[1] - tgt[1]


def package_version_series(value):
    """从包版本中提取 1.34.2 元组。"""
    m = re.match(r"^(\d+\.\d+\.\d+)", str(value))
    if not m:
        return None
    return parse_version_tuple(m.group(1))


def select_package_version(candidates, requested):
    """从包管理器列出的版本中选择目标版本。

    requested: VersionRequest
    返回 (package_version, k8s_version)
    """
    parsed = []
    for raw in candidates:
        raw = str(raw).strip()
        if not raw:
            continue
        kv = package_version_series(raw)
        if not kv:
            continue
        if kv[1] not in K8S_MINORS:
            continue
        parsed.append((raw, kv))
    if not parsed:
        raise ValidationError("软件源中找不到 Kubernetes 1.28-1.37 的 kubeadm 包")
    if requested.patch is not None:
        target = (1, requested.minor, requested.patch)
        matches = [(raw, kv) for raw, kv in parsed if kv == target]
    else:
        matches = [(raw, kv) for raw, kv in parsed if kv[1] == requested.minor]
    if not matches:
        raise ValidationError(
            "软件源中找不到 Kubernetes %s 的包" % requested.text,
            hint="请确认该版本已发布，或检查软件源/镜像同步状态",
        )
    matches.sort(key=lambda item: (item[1], package_version_key(item[0])), reverse=True)
    raw, kv = matches[0]
    return raw, "v%d.%d.%d" % kv


def upgrade_plan(current, target, allow_multi_minor=False):
    """生成 <= 1 个次版本的升级计划；多版本暂由 CLI 拒绝或逐次调用。"""
    step = validate_upgrade_path(current, target, allow_multi_minor)
    return [target]
