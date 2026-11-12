"""节点探测、预检与操作系统准备。"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile

from . import __version__
from .agent_base import (ActionFailure, backup_file, debug, ensure_dir, log,
                         read_file, read_os_release, remove_file, run, run_ok,
                         service_active, write_file)
from .compat import evaluate, os_family
from .constants import (AGENT_STATE_DIR, DEFAULT_TIMEZONE,
                        DEFAULT_VIP_PORT, KERNEL_MODULES, MASTER_TCP_PORTS,
                        MASTER_UDP_PORTS, SYSCTL_VALUES, WORKER_TCP_PORTS,
                        WORKER_UDP_PORTS)
from .util import parse_version_tuple, tail, valid_dns_subdomain


def _read_meminfo():
    data = {}
    for line in read_file("/proc/meminfo").splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            data[key.strip()] = val.strip()
    return data


def _kb_to_mb(value):
    try:
        return int(re.match(r"(\d+)", value).group(1)) // 1024
    except Exception:
        return 0


def _detect_ips():
    result = []
    seen = set()
    proc = run_ok(["ip", "-o", "-4", "addr", "show"], timeout=15)
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"\d+:\s+([^\s@]+)(?:@\S+)?\s+inet\s+([0-9.]+)/(\d+)", line)
        if not m:
            continue
        iface, ip, prefix = m.group(1), m.group(2), m.group(3)
        if ip in seen:
            continue
        seen.add(ip)
        result.append({"interface": iface, "ip": ip, "prefix": int(prefix),
                       "cidr": "%s/%s" % (ip, prefix)})
    if not result:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip not in seen and not ip.startswith("127."):
                    seen.add(ip)
                    result.append({"interface": "", "ip": ip, "prefix": 24, "cidr": ip + "/24"})
        except OSError:
            pass
    return result


def _default_iface():
    proc = run_ok(["ip", "route", "show", "default"], timeout=10)
    m = re.search(r"\bdev\s+(\S+)", proc.stdout or "")
    return m.group(1) if m else ""


def _interface_cidr(ips, iface):
    for item in ips:
        if item["interface"] == iface:
            return item["cidr"]
    return ""


def _listening_ports(udp=False):
    ports = set()
    proc = run_ok(["ss", "-H", "-lnu" if udp else "-lnt"], timeout=15)
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 4:
            local = fields[3]
            port = local.rsplit(":", 1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return ports


def _expand_ports(specs):
    result = set()
    for item in specs:
        item = str(item)
        if "-" in item:
            try:
                start, end = item.split("-", 1)
                result.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        elif item.isdigit():
            result.add(int(item))
    return result


def _cmd_version(command):
    proc = run_ok(command, timeout=20)
    text = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
    return text.splitlines()[0] if text else ""


def _parse_containerd_version():
    text = _cmd_version(["containerd", "--version"])
    m = re.search(r"containerd\s+v?([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else ""


def _parse_runc_version():
    text = _cmd_version(["runc", "--version"])
    m = re.search(r"version\s+v?([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else ""


def _os_family(osr):
    os_id = (osr.get("ID") or "").lower()
    id_like = (osr.get("ID_LIKE") or "").lower()
    if os_id in ("ubuntu", "debian") or "debian" in id_like:
        return "debian"
    if os_id in ("centos", "rhel", "rocky", "almalinux", "ol", "fedora", "openeuler") or \
            "rhel" in id_like or "fedora" in id_like:
        return "rhel"
    if shutil.which("apt-get"):
        return "debian"
    if shutil.which("dnf") or shutil.which("yum"):
        return "rhel"
    return ""


def _package_manager(family):
    if family == "debian" and shutil.which("apt-get"):
        return "apt"
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("yum"):
        return "yum"
    return ""


def _selinux():
    if not shutil.which("getenforce"):
        return "Disabled" if not os.path.isdir("/sys/fs/selinux") else "Unknown"
    proc = run_ok(["getenforce"], timeout=10)
    return (proc.stdout or "Unknown").strip() or "Unknown"


def _swap_mb():
    info = _read_meminfo()
    total = 0
    for key, val in info.items():
        if key.startswith("SwapTotal"):
            total += _kb_to_mb(val)
    return total


def _disk_free_gb(path="/var/lib"):
    try:
        usage = shutil.disk_usage(path)
        return round(usage.free / float(1024 ** 3), 1)
    except OSError:
        return 0.0


def _time_sync():
    proc = run_ok(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
    value = (proc.stdout or "").strip().lower()
    if value in ("yes", "true"):
        return True
    if value in ("no", "false"):
        return False
    return None


def _virt_type():
    proc = run_ok(["systemd-detect-virt"], timeout=10)
    value = (proc.stdout or "").strip()
    if proc.returncode != 0 or value == "none":
        return ""
    return value


def _cloud_hint():
    """尽力识别公有云/虚拟化类型；仅用于提示 VIP/负载均衡方案。"""
    product = read_file("/sys/class/dmi/id/product_name").strip()
    vendor = read_file("/sys/class/dmi/id/sys_vendor").strip()
    text = (product + " " + vendor).lower()
    virt = ""
    proc = run_ok(["systemd-detect-virt"], timeout=10)
    if proc.returncode == 0:
        virt = (proc.stdout or "").strip()
    checks = [
        ("amazon", "AWS EC2"), ("google", "Google Compute Engine"),
        ("microsoft", "Microsoft Azure/Hyper-V"), ("alibaba", "Alibaba Cloud"),
        ("aliyun", "Alibaba Cloud"), ("tencent", "Tencent Cloud"),
        ("huawei", "Huawei Cloud"), ("openstack", "OpenStack"),
        ("oracle", "Oracle Cloud"), ("ibm", "IBM Cloud"),
        ("vmware", "VMware"), ("kvm", "KVM"), ("xen", "Xen"),
    ]
    for key, label in checks:
        if key in text:
            return label
    if virt in ("amazon", "google", "microsoft", "alibaba", "tencent", "huawei", "openstack"):
        return virt
    if virt in ("vmware", "kvm", "xen"):
        return virt
    return ""


def _current_cluster():
    installed = any(os.path.exists(p) for p in (
        "/etc/kubernetes/admin.conf", "/etc/kubernetes/kubelet.conf",
        "/var/lib/kubelet/config.yaml"))
    version = ""
    if installed:
        kubelet = _cmd_version(["kubelet", "--version"])
        m = re.search(r"v?([0-9]+\.[0-9]+\.[0-9]+)", kubelet)
        version = "v" + m.group(1) if m else ""
    return {
        "installed": installed,
        "version": version,
        "kubelet_active": service_active("kubelet"),
        "admin_conf": os.path.exists("/etc/kubernetes/admin.conf"),
        "etcd_data": os.path.isdir("/var/lib/etcd/member"),
    }


def detect():
    osr = read_os_release()
    family = _os_family(osr)
    arch_raw = os.uname().machine
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
            "arm64": "arm64"}.get(arch_raw, arch_raw)
    ips = _detect_ips()
    iface = _default_iface()
    meminfo = _read_meminfo()
    ports = _listening_ports()
    udp_ports = _listening_ports(udp=True)
    existing = _current_cluster()
    fw_firewalld = service_active("firewalld")
    fw_ufw = False
    if shutil.which("ufw"):
        proc = run_ok(["ufw", "status"], timeout=10)
        fw_ufw = "Status: active" in (proc.stdout or "")
    cgroup = "2" if os.path.exists("/sys/fs/cgroup/cgroup.controllers") else "1"
    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "os_id": (osr.get("ID") or "").lower(),
        "os_name": osr.get("PRETTY_NAME") or osr.get("NAME") or "Linux",
        "os_version": osr.get("VERSION_ID") or "",
        "os_family": family,
        "pkg_mgr": _package_manager(family),
        "arch": arch,
        "arch_raw": arch_raw,
        "kernel": os.uname().release,
        "cgroup_version": cgroup,
        "systemd": os.path.isdir("/run/systemd/system"),
        "is_root": os.geteuid() == 0,
        "python_version": "%d.%d.%d" % tuple(__import__("sys").version_info[:3]),
        "cpu_count": os.cpu_count() or 0,
        "memory_mb": _kb_to_mb(meminfo.get("MemTotal", "0 kB")),
        "swap_total_mb": _swap_mb(),
        "disk_free_gb": _disk_free_gb(),
        "ips": ips,
        "default_iface": iface,
        "default_iface_cidr": _interface_cidr(ips, iface),
        "selinux": _selinux(),
        "firewall_active": fw_firewalld or fw_ufw,
        "firewalld": fw_firewalld,
        "ufw": fw_ufw,
        "time_synced": _time_sync(),
        "existing_cluster": existing,
        "kubelet_active": existing["kubelet_active"],
        "kubeadm_version": _cmd_version(["kubeadm", "version", "-o", "short"]),
        "kubelet_version": _cmd_version(["kubelet", "--version"]),
        "containerd_version": _parse_containerd_version(),
        "runc_version": _parse_runc_version(),
        "crictl_version": _cmd_version(["crictl", "--version"]),
        "docker_active": service_active("docker") or service_active("docker.service"),
        "ports_in_use": sorted(ports),
        "master_ports_busy": sorted(_expand_ports(MASTER_TCP_PORTS) & ports),
        "worker_ports_busy": sorted(_expand_ports(WORKER_TCP_PORTS) & ports),
        "endpoint_port_busy": 16443 in ports,
        "udp_8472_busy": 8472 in udp_ports,
        "udp_4789_busy": 4789 in udp_ports,
        "virtualization": _virt_type(),
        "cloud_hint": _cloud_hint(),
        "kube_deploy_state": os.path.exists(os.path.join(AGENT_STATE_DIR, "state.json")),
    }


def preflight(params):
    facts = detect()
    target_version = params.get("version") or ""
    parsed = parse_version_tuple(target_version)
    if not parsed:
        raise ActionFailure("版本参数无效：%s" % target_version)
    target_minor = parsed[1]
    role = params.get("role") or "master"
    mode = params.get("mode") or "install"
    node_ip = params.get("node_ip") or ""
    errors, warnings = evaluate(
        facts, target_minor, role=role, mode=mode,
        allow_untested_os=bool(params.get("allow_untested_os")),
        single_node=bool(params.get("single_node")),
        internal_ha=bool(params.get("ha")),
    )
    if mode in ("install", "expand"):
        if not node_ip:
            errors.append("必须指定节点 IP")
        else:
            ips = {x["ip"] for x in facts["ips"]}
            if node_ip not in ips:
                errors.append(
                    "指定 IP %s 不属于节点本地地址（本地地址：%s）" % (
                        node_ip, ", ".join(sorted(ips)) or "无"))
                errors.append("请使用节点私网 IP；公有云 NAT 场景可写 公网IP=私网IP，或启用自动私网 IP 探测")
        if facts.get("existing_cluster", {}).get("installed"):
            errors.append("节点已有 Kubernetes 配置，安装会被拒绝；如需重装请先执行 reset")
    if role == "master" and mode in ("install", "expand"):
        busy = set(facts.get("master_ports_busy") or [])
        # 6443/2379/2380/10257/10259 是控制平面端口；10250 可能被旧 kubelet 占用
        if busy:
            errors.append("master 所需端口已被占用：%s" % ", ".join(str(x) for x in sorted(busy)))
        vip_port = int(params.get("vip_port") or DEFAULT_VIP_PORT)
        if params.get("ha") and vip_port in set(facts.get("ports_in_use") or []):
            errors.append("HAProxy 端点端口 %d 已被占用" % vip_port)
    cni = params.get("cni") or "calico"
    if mode in ("install", "expand") and cni == "flannel" and facts.get("udp_8472_busy"):
        errors.append(
            "Flannel VXLAN 端口 8472/udp 已被占用；可能是已有集群或 reset 后残留的 Flannel。"
            "请先用 status/kubectl 确认；确认重装时执行 reset，或停止残留 Flannel 容器")
    if (mode in ("install", "expand") and cni == "calico"
            and params.get("calico_encapsulation") == "vxlan"
            and facts.get("udp_4789_busy")):
        errors.append("Calico VXLAN 端口 4789/udp 已被占用；请检查残留 Calico 容器")
    if role == "worker" and mode in ("install", "expand"):
        busy = set(facts.get("worker_ports_busy") or [])
        if busy:
            errors.append("worker 所需端口已被占用：%s" % ", ".join(str(x) for x in sorted(busy)))
    if facts.get("docker_active"):
        warnings.append("检测到 Docker 服务；本工具使用 containerd，请确认 Docker 不会占用 containerd 命名空间/端口")
    if facts.get("time_synced") is False:
        warnings.append("系统时间未同步，etcd/证书校验可能失败；请配置 NTP/chrony")
    if not facts.get("pkg_mgr"):
        errors.append("未找到受支持的包管理器（apt-get/dnf/yum）")
    if facts.get("pkg_mgr") == "apt":
        tmp_ok = False
        var_tmp_ok = False
        for path, label in (("/tmp", "tmp"), ("/var/tmp", "var_tmp")):
            try:
                fd, probe = tempfile.mkstemp(prefix=".kubedeploy-", dir=path)
                os.close(fd); os.unlink(probe)
                if label == "tmp": tmp_ok = True
                else: var_tmp_ok = True
            except OSError:
                pass
        try:
            mode = os.stat("/tmp").st_mode & 0o7777
        except OSError:
            mode = 0
        if not var_tmp_ok:
            errors.append("/var/tmp 不可写，APT 无法创建临时配置")
        if not tmp_ok or not (mode & 0o1000):
            warnings.append("/tmp 不可写或缺少 sticky 位；prepare 阶段会尝试 "
                            "chmod 1777 /tmp，APT 同时使用 /var/tmp")
    if not facts.get("systemd"):
        errors.append("未检测到 systemd")
    if not facts.get("hostname"):
        errors.append("无法读取 hostname")
    hostname = facts.get("hostname") or ""
    node_name = (params.get("node_name") or hostname).strip().lower()
    if not valid_dns_subdomain(node_name):
        warnings.append("Kubernetes 节点名 %r 不是合法 DNS subdomain；控制端会尝试规范化" % node_name)
    elif node_name.startswith("localhost"):
        warnings.append("hostname %r 使用了 localhost 名称；控制端将使用基于节点 IP 的节点名" % hostname)
    if facts.get("swap_total_mb") and not params.get("keep_swap"):
        warnings.append("swap 已启用；prepare 阶段将关闭并持久化禁用")
    return {
        "facts": facts,
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "target_version": target_version,
            "role": role,
            "mode": mode,
            "node_ip": node_ip,
            "cgroup_v2": facts.get("cgroup_version") == "2",
        },
    }


def _disable_swap():
    changes = []
    proc = run_ok(["swapoff", "-a"], timeout=120)
    if not proc.returncode == 0:
        changes.append("swapoff -a 返回非零（可能已禁用）")
    fstab = "/etc/fstab"
    text = read_file(fstab)
    if text:
        backup_file(fstab)
        lines = []
        changed = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped.split()) >= 3 and stripped.split()[2] == "swap":
                lines.append("# kube-deploy disabled swap: " + line)
                changed = True
            else:
                lines.append(line)
        if changed:
            write_file(fstab, "\n".join(lines) + "\n")
            changes.append("已注释 /etc/fstab 中的 swap 条目")
    return changes


def _prepare_selinux(mode):
    changes = []
    current = _selinux()
    if current == "Enforcing" and mode == "permissive":
        cfg = "/etc/selinux/config"
        text = read_file(cfg)
        if text:
            backup_file(cfg)
            new = re.sub(r"(?m)^SELINUX=.*$", "SELINUX=permissive", text)
            if new != text:
                write_file(cfg, new)
                changes.append("SELINUX=permissive（下次启动生效）")
        proc = run_ok(["setenforce", "0"], timeout=30)
        if proc.returncode == 0:
            changes.append("SELinux 已临时切换为 permissive")
    elif current == "Enforcing" and mode == "enforcing":
        changes.append("保持 SELinux Enforcing；请确认容器运行时和 kubelet 的 SELinux 策略")
    return changes


def _ensure_tmp_permissions():
    """修复 Ubuntu 云镜像常见的 /tmp 0755；apt-key 会降权到 _apt 用户。"""
    changes = []
    warnings = []
    for path in ("/tmp", "/var/tmp"):
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        try:
            mode = os.stat(path).st_mode & 0o7777
            if mode != 0o1777:
                os.chmod(path, 0o1777)
                changes.append("%s 权限已修正为 1777（原 %o）" % (path, mode))
        except OSError as exc:
            warnings.append("无法修正 %s 权限：%s" % (path, exc))
    return changes, warnings


def _cleanup_legacy_flannel():
    """切换 Calico 前清掉 reset 后残留的 Flannel 接口/CNI 配置。"""
    changes = []
    for iface in ("flannel.1", "cni0"):
        proc = run_ok(["ip", "link", "delete", iface], timeout=30)
        if proc.returncode == 0:
            changes.append("已删除残留接口 %s" % iface)
    for path in ("/etc/cni/net.d/10-flannel.conflist",
                 "/etc/cni/net.d/10-flannel.conf"):
        if os.path.exists(path):
            remove_file(path)
            changes.append("已删除残留 CNI 配置 %s" % path)
    return changes


def _configure_firewall(role, ha, node_port_range="30000-32767",
                        cni="calico", encapsulation="ipip", vip_port=16443):
    changes = []
    tcp_ports = list(MASTER_TCP_PORTS if role == "master" else WORKER_TCP_PORTS)
    udp_ports = []
    if cni == "flannel":
        udp_ports.append("8472")
    else:
        tcp_ports.append("179")
        udp_ports.append("4789")
    if role == "master" and ha:
        udp_ports.append("112")
    if service_active("firewalld"):
        for port in tcp_ports:
            run_ok(["firewall-cmd", "--permanent", "--add-port=%s/tcp" % port], timeout=30)
        for port in udp_ports:
            run_ok(["firewall-cmd", "--permanent", "--add-port=%s/udp" % port], timeout=30)
        run_ok(["firewall-cmd", "--permanent", "--add-port=%s/tcp" % node_port_range], timeout=30)
        if role == "master" and ha:
            run_ok(["firewall-cmd", "--permanent", "--add-port=%d/tcp" % vip_port], timeout=30)
            run_ok(["firewall-cmd", "--permanent", "--add-protocol=vrrp"], timeout=30)
        if cni == "calico" and encapsulation == "ipip":
            proc = run_ok(["firewall-cmd", "--permanent", "--add-protocol=ipip"], timeout=30)
            if proc.returncode != 0:
                changes.append("firewalld 未能直接放行 IPIP；请手工允许 IP 协议 4")
        run(["firewall-cmd", "--reload"], timeout=60, check=False)
        changes.append("firewalld 已放行 Kubernetes/%s 端口" % cni)
    elif shutil.which("ufw"):
        proc = run_ok(["ufw", "status"], timeout=10)
        if "Status: active" in (proc.stdout or ""):
            for port in tcp_ports:
                spec = str(port).replace("-", ":")
                run_ok(["ufw", "allow", "%s/tcp" % spec], timeout=30)
            for port in udp_ports:
                spec = port.replace("-", ":")
                run_ok(["ufw", "allow", "%s/udp" % spec], timeout=30)
            node_ports = str(node_port_range).replace("-", ":")
            run_ok(["ufw", "allow", "%s/tcp" % node_ports], timeout=30)
            if role == "master" and ha:
                run_ok(["ufw", "allow", "%d/tcp" % vip_port], timeout=30)
                run_ok(["ufw", "allow", "proto", "vrrp"], timeout=30)
            if cni == "calico" and encapsulation == "ipip":
                proc = run_ok(["ufw", "allow", "proto", "ipip"], timeout=30)
                if proc.returncode != 0:
                    changes.append("ufw 未能直接放行 IPIP；请手工允许 IP 协议 4")
            changes.append("ufw 已放行 Kubernetes/%s 端口" % cni)
    return changes


def prepare(params):
    changes = []
    warnings = []
    role = params.get("role") or "master"
    ha = bool(params.get("ha"))
    keep_swap = bool(params.get("keep_swap"))
    tmp_changes, tmp_warnings = _ensure_tmp_permissions()
    changes.extend(tmp_changes)
    warnings.extend(tmp_warnings)

    # kernel modules
    ensure_dir("/etc/modules-load.d", 0o755)
    write_file("/etc/modules-load.d/kube-deploy.conf",
               "\n".join(KERNEL_MODULES) + "\n")
    for module in KERNEL_MODULES:
        if os.path.isdir("/sys/module/%s" % module):
            continue
        proc = run_ok(["modprobe", module], timeout=30)
        if proc.returncode != 0:
            raise ActionFailure("无法加载内核模块 %s" % module,
                                errors=[tail(proc.stderr, 5)],
                                hint="请确认内核支持 overlay/br_netfilter")
    changes.append("已加载 overlay/br_netfilter")

    # sysctl
    lines = ["# Managed by KubeDeploy"]
    for key, value in SYSCTL_VALUES.items():
        lines.append("%s = %s" % (key, value))
    write_file("/etc/sysctl.d/99-kube-deploy.conf", "\n".join(lines) + "\n")
    proc = run(["sysctl", "--system"], timeout=60, check=False)
    if proc.returncode != 0:
        warnings.append("sysctl --system 返回非零，请检查内核参数")
    for key, value in SYSCTL_VALUES.items():
        current = read_file("/proc/sys/" + key.replace(".", "/")).strip()
        if current and current != value:
            warnings.append("sysctl %s=%s 未生效（当前 %s）" % (key, value, current))
    changes.append("Kubernetes sysctl 已持久化")

    if not keep_swap:
        changes.extend(_disable_swap())
        swap_show = run_ok(["swapon", "--show"], timeout=30)
        if (swap_show.stdout or "").strip():
            raise ActionFailure("swap 仍然处于启用状态，kubeadm 预检会失败",
                                errors=[swap_show.stdout.strip()[-600:]],
                                hint="请关闭 zram/交换分区并检查 /etc/fstab、systemd swap 单元")
    else:
        warnings.append("已按 --keep-swap 保留 swap；kubeadm 预检很可能失败")

    changes.extend(_prepare_selinux(params.get("selinux_mode") or "permissive"))

    tz = params.get("timezone") or DEFAULT_TIMEZONE
    if tz and shutil.which("timedatectl"):
        proc = run_ok(["timedatectl", "set-timezone", tz], timeout=30)
        if proc.returncode == 0:
            changes.append("时区已设置为 %s" % tz)
        else:
            warnings.append("时区设置失败：%s" % (proc.stderr.strip()[-300:] or tz))

    if role == "master":
        profile = "/etc/profile.d/kubectl.sh"
        write_file(profile, """# Managed by KubeDeploy
if command -v kubectl >/dev/null 2>&1; then
    source <(kubectl completion bash)
    alias k=kubectl
    complete -o default -F __start_kubectl k
fi
if [ -f /etc/kubernetes/admin.conf ]; then
    export KUBECONFIG=/etc/kubernetes/admin.conf
fi
""", 0o644)
        changes.append("已写入 kubectl 补全/KUBECONFIG profile")

    if (params.get("cni") or "calico") == "calico":
        changes.extend(_cleanup_legacy_flannel())

    # hostname 解析（OS 主机名和 Kubernetes 节点名分别处理）
    node_ip = params.get("node_ip") or ""
    hostname = socket.gethostname()
    node_name = (params.get("node_name") or hostname).strip()
    if node_ip:
        pairs = ((hostname, "# kube-deploy"),) if node_name == hostname else (
            (node_name, "# kube-deploy-node"),)
        for name, marker in pairs:
            if not name:
                continue
            proc = run_ok(["getent", "hosts", name], timeout=10)
            resolved = (proc.stdout or "").strip()
            if not resolved:
                hosts = "/etc/hosts"
                backup_file(hosts)
                text = read_file(hosts)
                if marker not in text:
                    write_file(hosts, text.rstrip() + "\n%s %s  %s\n" % (node_ip, name, marker))
                    changes.append("已写入 /etc/hosts：%s -> %s" % (name, node_ip))
            elif node_ip not in resolved:
                warnings.append("/etc/hosts 中 %s 的解析不包含 %s，请检查多网卡环境" % (name, node_ip))

    if params.get("configure_firewall", True):
        changes.extend(_configure_firewall(
            role, ha, params.get("node_port_range") or "30000-32767",
            params.get("cni") or "calico",
            params.get("calico_encapsulation") or "ipip",
            int(params.get("vip_port") or DEFAULT_VIP_PORT)))

    if _selinux() == "Enforcing":
        warnings.append("SELinux 仍为 Enforcing；若 kubelet/容器异常，请检查 SELinux 策略")
    if shutil.which("timedatectl") and _time_sync() is False:
        warnings.append("时间未同步，建议在安装前启用 chrony/chronyd")
    return {"changes": changes, "warnings": warnings, "facts": detect()}
