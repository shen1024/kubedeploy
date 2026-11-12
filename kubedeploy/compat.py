"""跨发行版兼容性判断（CLI 与远程 agent 共用）。"""

from __future__ import annotations

from .constants import (SUPPORTED_ARCH, SUPPORTED_DEBIAN, SUPPORTED_EL_MAJOR,
                        SUPPORTED_OS_FAMILIES, SUPPORTED_UBUNTU, MIN_MEM_MB,
                        RECOMMENDED_MEM_MB, MIN_DISK_GB)
from .util import parse_version_tuple


def kernel_tuple(release):
    nums = []
    for part in (release or "").split("-")[0].split("."):
        try:
            nums.append(int(part))
        except ValueError:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def os_family(facts):
    return (facts.get("os_family") or "").lower()


def os_label(facts):
    name = facts.get("os_name") or facts.get("os_id") or "Linux"
    ver = facts.get("os_version") or ""
    return ("%s %s" % (name, ver)).strip()


def _version_major(version):
    try:
        return int(str(version).split(".")[0])
    except (ValueError, AttributeError):
        return None


def check_os(facts, allow_untested_os=False):
    """返回 (errors, warnings)。"""
    errors = []
    warnings = []
    family = os_family(facts)
    os_id = (facts.get("os_id") or "").lower()
    version = str(facts.get("os_version") or "")
    version_major = _version_major(version)
    label = os_label(facts)

    if not facts.get("systemd"):
        errors.append("未检测到 systemd；KubeDeploy 仅支持以 systemd 管理的 Linux 发行版")
    if family not in SUPPORTED_OS_FAMILIES:
        msg = "未经充分验证的发行版：%s（family=%s）" % (label, family or "unknown")
        (warnings if allow_untested_os else errors).append(
            msg + ("；已使用 --allow-untested-os 继续" if allow_untested_os else
                   "；如确认兼容，请增加 --allow-untested-os"))
        return errors, warnings

    if os_id == "ubuntu":
        if version not in SUPPORTED_UBUNTU:
            warnings.append("Ubuntu %s 不在官方验证矩阵（20.04/22.04/24.04/26.04）" % version)
    elif os_id == "debian":
        if version_major is None or str(version_major) not in SUPPORTED_DEBIAN:
            warnings.append("Debian %s 不在推荐版本范围（11/12/13）" % version)
    else:
        if version_major not in SUPPORTED_EL_MAJOR:
            warnings.append("%s %s 不在验证矩阵（EL 7/8/9/10）" % (label, version))
        if version_major == 7:
            warnings.append("CentOS/RHEL 7 已 EOL，仅建议用于存量环境；Kubernetes 1.35+ 无法在 cgroup v1 上运行")
        if version_major == 8:
            warnings.append("EL8 默认 cgroup v1；部署 Kubernetes 1.35+ 前必须切换到 cgroup v2 并升级内核")
    if os_id in ("centos", "rhel", "rocky", "almalinux", "ol"):
        if version_major is not None and version_major <= 7:
            warnings.append("%s 已停止维护，请确保已配置可用的 vault/内部软件源" % label)
    return errors, warnings


def check_target_k8s(facts, target_minor, target_version=None):
    errors = []
    warnings = []
    cgroup = str(facts.get("cgroup_version") or "")
    kernel = kernel_tuple(facts.get("kernel"))
    os_id = (facts.get("os_id") or "").lower()
    version_major = _version_major(facts.get("os_version"))

    if target_minor >= 35:
        if cgroup != "2":
            errors.append(
                "Kubernetes 1.%d 要求 cgroup v2，但节点当前使用 cgroup v%s" % (target_minor, cgroup or "?"),
                )
            errors[-1] += "；请在操作系统层启用 cgroup v2（通常需修改内核启动参数并重启）"
        if kernel < (5, 8, 0):
            warnings.append(
                "节点内核 %s 低于 Kubernetes 推荐的 5.8；cgroup v2 在旧内核上可能不稳定" %
                facts.get("kernel", "unknown"))
        if os_id in ("centos", "rhel", "rocky", "almalinux", "ol") and version_major == 7:
            errors.append("EL7 内核 3.10 不支持 cgroup v2，无法运行 Kubernetes 1.%d" % target_minor)
        if os_id == "ubuntu" and version_major is not None and str(facts.get("os_version")) == "20.04":
            warnings.append("Ubuntu 20.04 内核 5.4 低于 cgroup v2 的推荐 5.8，1.35+ 仅建议在升级内核后使用")
    elif target_minor >= 31:
        if cgroup == "1":
            warnings.append("Kubernetes 1.%d 仍支持 cgroup v1，但已进入维护模式，建议迁移到 cgroup v2" % target_minor)
    if target_minor >= 34 and kernel < (4, 19, 0):
        warnings.append("Kubernetes 1.%d 在 4.19 以下内核上属于未验证组合" % target_minor)
    if target_minor >= 37 and os_id == "ubuntu" and str(facts.get("os_version")) == "20.04":
        warnings.append("Ubuntu 20.04 不属于 Kubernetes 1.37 的企业推荐组合（内核 5.4，cgroup v2 支持有限）")
    return errors, warnings


def check_arch(facts):
    arch = facts.get("arch") or ""
    if arch not in SUPPORTED_ARCH.values():
        return ["不支持的 CPU 架构：%s（仅支持 amd64/x86_64 与 arm64/aarch64）" % (arch or "unknown")]
    return []


def check_resources(facts, single_node=False):
    errors = []
    warnings = []
    mem = int(facts.get("memory_mb") or 0)
    disk = float(facts.get("disk_free_gb") or 0)
    cpu = int(facts.get("cpu_count") or 0)
    if mem and mem < MIN_MEM_MB:
        errors.append("内存不足：%s MB，最低要求 %s MB" % (mem, MIN_MEM_MB))
    elif mem and mem < RECOMMENDED_MEM_MB:
        warnings.append("内存 %s MB 低于推荐值 %s MB" % (mem, RECOMMENDED_MEM_MB))
    if disk and disk < MIN_DISK_GB:
        errors.append("磁盘可用空间不足：%.1f GB，最低要求 %s GB" % (disk, MIN_DISK_GB))
    if cpu and cpu < 2:
        warnings.append("CPU 少于 2 核，kubeadm 预检可能失败")
    if single_node and mem and mem < 4096:
        warnings.append("单节点集群同时承载工作负载，建议至少 4 GB 内存")
    return errors, warnings


def evaluate(facts, target_minor, role="master", mode="install",
             allow_untested_os=False, single_node=False, internal_ha=False):
    """汇总兼容性错误/警告。"""
    errors, warnings = [], []
    if mode in ("install", "expand"):
        if not facts.get("is_root"):
            errors.append("需要 root 权限（远程节点请使用 root 或配置免密 sudo）")
        existing = facts.get("existing_cluster") or {}
        if existing.get("installed"):
            errors.append(
                "节点已存在 Kubernetes 集群（%s）；请使用扩容/升级功能，或先执行 kubeadm reset" %
                (existing.get("version") or "未知版本"))
        if facts.get("kubelet_active"):
            errors.append("kubelet 服务正在运行，节点可能已是集群成员")
    e, w = check_os(facts, allow_untested_os=allow_untested_os)
    errors.extend(e); warnings.extend(w)
    errors.extend(check_arch(facts))
    e, w = check_target_k8s(facts, target_minor)
    errors.extend(e); warnings.extend(w)
    if mode in ("install", "expand"):
        e, w = check_resources(facts, single_node=single_node)
        errors.extend(e); warnings.extend(w)
    if facts.get("swap_total_mb") and not facts.get("swap_disabled"):
        warnings.append("节点启用了 swap；安装流程将关闭并持久禁用（可用 --keep-swap 保留，但 kubeadm 会失败）")
    if facts.get("selinux") == "Enforcing":
        if mode == "install":
            warnings.append("SELinux 为 Enforcing；安装流程将切换为 permissive（不重启）")
    if facts.get("firewall_active"):
        warnings.append("检测到活动防火墙；安装流程将按角色放行 Kubernetes 所需端口；"
                        "公有云安全组仍需单独放行集群端口")
    cloud = (facts.get("cloud_hint") or "").lower()
    if internal_ha and cloud:
        if any(k in cloud for k in ("amazon", "aws", "google", "gcp", "microsoft",
                                    "azure", "alibaba", "aliyun", "tencent", "huawei")):
            warnings.append(
                "检测到公有云/虚拟化环境（%s）：Keepalived VIP 通常需要云厂商 HAVIP/ENI "
                "等二层能力；生产环境建议改用外部 LB：--control-plane-endpoint <LB>:6443 --external-endpoint"
                % facts.get("cloud_hint"))
        else:
            warnings.append(
                "检测到虚拟化环境（%s）：请确认 VRRP/混杂模式/伪造报文策略允许 Keepalived VIP"
                % facts.get("cloud_hint"))
    if facts.get("virtualization") and not cloud:
        warnings.append("检测到虚拟化环境（%s）；虚拟机使用内置 VIP 前请验证 VRRP/ARP 能力"
                        % facts.get("virtualization"))
    if role == "master" and facts.get("ports_busy"):
        errors.append("控制平面端口已被占用：%s" % ", ".join(facts["ports_busy"]))
    if role == "worker" and facts.get("worker_ports_busy"):
        errors.append("worker 端口已被占用：%s" % ", ".join(facts["worker_ports_busy"]))
    return errors, warnings


def compatibility_matrix():
    """供文档/CLI 输出的简表。"""
    return [
        ("Kubernetes 1.28-1.30", "CentOS 7-10 / Ubuntu 20.04-26.04", "cgroup v1/v2", "可用；旧版本已 EOL，建议仅存量"),
        ("Kubernetes 1.31-1.34", "CentOS 7-10 / Ubuntu 20.04-26.04", "cgroup v1 维护模式", "CentOS 7 可用但不再推荐"),
        ("Kubernetes 1.35-1.37", "CentOS 9/10、Ubuntu 22.04-26.04（cgroup v2 + 内核 5.8+ 推荐）", "必须 cgroup v2", "CentOS 7/8 与默认 cgroup v1 的系统不可用"),
    ]
