"""CLI 侧数据模型。"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .constants import (DEFAULT_CALICO_ENCAPSULATION,
                        DEFAULT_CALICO_IMAGE_REPOSITORY, DEFAULT_CALICO_VERSION,
                        DEFAULT_CNI, DEFAULT_DNS_DOMAIN,
                        DEFAULT_FLANNEL_CNI_IMAGE, DEFAULT_FLANNEL_IMAGE,
                        DEFAULT_IMAGE_REPOSITORY, DEFAULT_POD_CIDR,
                        DEFAULT_SERVICE_CIDR, DEFAULT_TIMEZONE,
                        DEFAULT_VIP_PORT)
from .util import normalize_address, safe_slug, utc_now, valid_dns_label


ROLE_MASTER = "master"
ROLE_WORKER = "worker"


@dataclass
class SshOptions:
    user: str = "root"
    port: int = 22
    key: Optional[str] = None
    password: Optional[str] = None
    proxy_jump: Optional[str] = None
    strict_host_key_checking: str = "accept-new"
    known_hosts: Optional[str] = None
    extra_options: List[str] = field(default_factory=list)
    connect_timeout: int = 20
    become: bool = True

    def public_dict(self):
        data = asdict(self)
        data.pop("password", None)
        return data


@dataclass
class NodeSpec:
    address: str
    role: str = ROLE_MASTER
    hostname: str = ""
    node_ip: str = ""
    arch: str = ""
    os_id: str = ""
    os_version: str = ""
    facts: dict = field(default_factory=dict)
    local: bool = False

    @property
    def k8s_ip(self):
        """Kubernetes/kubeadm 使用的节点地址；SSH 地址可能不同（公有云 NAT）。"""
        return self.node_ip or self.address

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ClusterSpec:
    name: str
    version: str
    masters: List[NodeSpec]
    workers: List[NodeSpec]
    vip: Optional[str] = None
    vip_interface: Optional[str] = None
    vip_port: int = DEFAULT_VIP_PORT
    control_plane_endpoint: Optional[str] = None
    external_endpoint: bool = False
    pod_cidr: str = DEFAULT_POD_CIDR
    service_cidr: str = DEFAULT_SERVICE_CIDR
    dns_domain: str = DEFAULT_DNS_DOMAIN
    cni: str = DEFAULT_CNI
    timezone: str = DEFAULT_TIMEZONE
    calico_version: str = DEFAULT_CALICO_VERSION
    calico_image_repository: str = DEFAULT_CALICO_IMAGE_REPOSITORY
    calico_encapsulation: str = DEFAULT_CALICO_ENCAPSULATION
    image_repository: str = DEFAULT_IMAGE_REPOSITORY
    flannel_image: str = DEFAULT_FLANNEL_IMAGE
    flannel_cni_image: str = DEFAULT_FLANNEL_CNI_IMAGE
    pod_iface: Optional[str] = None
    ssh: SshOptions = field(default_factory=SshOptions)
    package_repo_base: Optional[str] = None
    allow_upstream_fallback: bool = True
    allow_control_plane_without_ha: bool = False
    disable_swap: bool = True
    selinux_mode: str = "permissive"
    configure_firewall: bool = True
    node_port_range: str = "30000-32767"
    created_at: str = field(default_factory=utc_now)

    @property
    def all_nodes(self):
        return list(self.masters) + list(self.workers)

    @property
    def node_count(self):
        return len(self.all_nodes)

    @property
    def master_count(self):
        return len(self.masters)

    @property
    def worker_count(self):
        return len(self.workers)

    @property
    def first_master(self):
        return self.masters[0]

    @property
    def endpoint(self):
        if self.control_plane_endpoint:
            return self.control_plane_endpoint
        if self.vip:
            return "%s:%d" % (self.vip, self.vip_port)
        if self.masters:
            # 无 VIP/外部 LB 时仅作为降级端点；validate() 默认会拒绝这种配置。
            return "%s:6443" % self.masters[0].k8s_ip
        raise ValueError("没有可用控制平面节点")

    @property
    def endpoint_url(self):
        ep = self.endpoint
        return ep if ep.startswith("https://") else "https://" + ep

    @property
    def use_internal_ha(self):
        return bool(self.vip) and not self.external_endpoint

    def validate(self):
        errors = []
        if not valid_dns_label(self.name):
            errors.append("集群名称必须为小写 DNS 标签（字母数字和 -，≤63 字符）")
        if self.cni not in ("calico", "flannel"):
            errors.append("CNI 必须是 calico 或 flannel")
        if self.calico_encapsulation not in ("ipip", "vxlan", "none"):
            errors.append("Calico 封装必须是 ipip、vxlan 或 none")
        if self.cni == "calico":
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$", self.calico_image_repository or ""):
                errors.append("Calico 镜像仓库地址非法")
            if not re.match(r"^v3\.\d+\.\d+$", self.calico_version or ""):
                errors.append("Calico 版本必须形如 v3.32.0")
        if not self.masters:
            errors.append("至少需要一个 master 节点")
        seen = set()
        seen_k8s = set()
        for node in self.all_nodes:
            if node.address in seen:
                errors.append("节点 SSH 地址重复：%s" % node.address)
            seen.add(node.address)
            if node.k8s_ip in seen_k8s:
                errors.append("节点 Kubernetes IP 重复：%s" % node.k8s_ip)
            seen_k8s.add(node.k8s_ip)
            if node.node_ip:
                try:
                    if ipaddress.ip_address(node.node_ip).version != 4:
                        errors.append("节点 Kubernetes IP 仅支持 IPv4：%s" % node.node_ip)
                except ValueError:
                    errors.append("节点 Kubernetes IP 非法：%s" % node.node_ip)
            if node.role not in (ROLE_MASTER, ROLE_WORKER):
                errors.append("节点角色错误：%s" % node.role)
        if len(self.masters) > 1:
            if not self.external_endpoint and not self.vip:
                if not self.allow_control_plane_without_ha:
                    errors.append(
                        "多 master 集群需要 --vip 或 --control-plane-endpoint（外部负载均衡），"
                        "否则控制平面没有故障切换能力"
                    )
            if self.vip and (self.vip in seen or self.vip in seen_k8s):
                errors.append("VIP 不能与节点 SSH 地址或 Kubernetes IP 相同")
            if self.external_endpoint and not self.control_plane_endpoint:
                errors.append("external_endpoint 需要 control_plane_endpoint")
        if self.vip and self.external_endpoint:
            errors.append("不能同时使用内部 VIP 和外部负载均衡端点")
        if not 1 <= int(self.vip_port) <= 65535:
            errors.append("VIP 端口必须在 1-65535 之间")
        if self.vip and int(self.vip_port) == 6443:
            errors.append("内部 VIP 端口不能为 6443（会与 kube-apiserver 冲突）；推荐 16443")
        if self.control_plane_endpoint and len(self.masters) == 1 and not self.external_endpoint:
            errors.append("单节点集群不需要外部端点；请移除 --control-plane-endpoint")
        # 防止把换行/构造字符注入 CNI YAML 或主机配置
        pod = svc = None
        try:
            pod = ipaddress.ip_network(self.pod_cidr, strict=False)
            svc = ipaddress.ip_network(self.service_cidr, strict=False)
            if pod.version != 4 or svc.version != 4:
                errors.append("当前版本仅支持 IPv4 Pod/Service CIDR")
            if pod.overlaps(svc):
                errors.append("Pod CIDR 与 Service CIDR 不能重叠")
            cloud_private = ipaddress.ip_network("192.168.0.0/16")
            if pod.overlaps(cloud_private) or svc.overlaps(cloud_private):
                errors.append("Pod/Service CIDR 不能与 192.168.0.0/16 重叠；"
                              "这会与常见云上私网 192.168.1.0/24 冲突")
            if pod.prefixlen < 8 or svc.prefixlen < 8:
                errors.append("CIDR 前缀过小，请使用合理范围")
        except ValueError as exc:
            errors.append("CIDR 无效：%s" % exc)
        import re as _re
        if self.vip:
            try:
                import ipaddress as _ip
                vip_obj = _ip.ip_address(self.vip)
                if vip_obj.version != 4:
                    errors.append("当前 HA 方案仅支持 IPv4 VIP")
                if pod is not None and svc is not None and (vip_obj in pod or vip_obj in svc):
                    errors.append("VIP 不能落在 Pod/Service CIDR 内")
            except ValueError:
                errors.append("VIP 不是合法 IPv4 地址：%s" % self.vip)
        if self.vip_interface and not _re.match(r"^[A-Za-z0-9_.:-]{1,32}$", self.vip_interface):
            errors.append("VIP 网卡名称非法")
        if self.pod_iface and not _re.match(r"^[A-Za-z0-9_.:-]{1,32}$", self.pod_iface):
            errors.append("Pod 网卡名称非法")
        for label, value in (("Flannel 镜像", self.flannel_image),
                             ("Flannel CNI 镜像", self.flannel_cni_image),
                             ("镜像仓库", self.image_repository)):
            if not value or not _re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$", value):
                errors.append("%s 含非法字符" % label)
        if self.control_plane_endpoint and not _re.match(
                r"^(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\]):\d{1,5}$",
                self.control_plane_endpoint):
            errors.append("control-plane-endpoint 必须是 host:port")
        if not _re.match(r"^\d{1,5}-\d{1,5}$", self.node_port_range or "30000-32767"):
            errors.append("node-port-range 格式应为 start-end")
        else:
            _a, _b = [int(x) for x in self.node_port_range.split("-", 1)]
            if not (1 <= _a <= _b <= 65535):
                errors.append("node-port-range 必须是 1-65535 内的升序范围")
        if errors:
            from .errors import ValidationError
            raise ValidationError("；".join(errors))
        return True

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "masters": [m.to_dict() for m in self.masters],
            "workers": [w.to_dict() for w in self.workers],
            "vip": self.vip,
            "vip_interface": self.vip_interface,
            "vip_port": self.vip_port,
            "control_plane_endpoint": self.control_plane_endpoint,
            "external_endpoint": self.external_endpoint,
            "pod_cidr": self.pod_cidr,
            "service_cidr": self.service_cidr,
            "dns_domain": self.dns_domain,
            "image_repository": self.image_repository,
            "flannel_image": self.flannel_image,
            "flannel_cni_image": self.flannel_cni_image,
            "pod_iface": self.pod_iface,
            "ssh": self.ssh.public_dict(),
            "package_repo_base": self.package_repo_base,
            "allow_upstream_fallback": self.allow_upstream_fallback,
            "allow_control_plane_without_ha": self.allow_control_plane_without_ha,
            "disable_swap": self.disable_swap,
            "selinux_mode": self.selinux_mode,
            "configure_firewall": self.configure_firewall,
            "node_port_range": self.node_port_range,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["masters"] = [NodeSpec.from_dict(x) for x in data.get("masters", [])]
        data["workers"] = [NodeSpec.from_dict(x) for x in data.get("workers", [])]
        ssh_data = data.pop("ssh", None) or {}
        spec = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if ssh_data:
            spec.ssh = SshOptions(**{k: v for k, v in ssh_data.items()
                                     if k in SshOptions.__dataclass_fields__})
        return spec


def parse_node_token(raw, role):
    """解析节点表达式：address 或 address=节点私网IP。

    示例：
      10.0.0.11
      47.100.10.11=172.16.0.11
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("节点地址不能为空")
    if "=" in raw:
        parts = raw.split("=")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError("节点表达式格式应为 SSH地址=私网IP：%s" % raw)
        ssh_addr = normalize_address(parts[0].strip())
        node_ip = parts[1].strip()
        try:
            if ipaddress.ip_address(node_ip).version != 4:
                raise ValueError
        except ValueError:
            raise ValueError("节点私网 IP 必须是 IPv4：%s" % node_ip)
        return NodeSpec(address=ssh_addr, role=role, node_ip=node_ip)
    return NodeSpec(address=normalize_address(raw), role=role)


def build_spec(args, interactive=False):
    """从 CLI 参数构建 ClusterSpec；不做网络探测。"""
    from .errors import ValidationError
    from .versions import normalize_version
    masters = []
    workers = []
    for raw in getattr(args, "masters", []) or []:
        masters.append(parse_node_token(raw, ROLE_MASTER))
    for raw in getattr(args, "workers", []) or []:
        workers.append(parse_node_token(raw, ROLE_WORKER))
    explicit_node_ip = getattr(args, "node_ip", None)
    if explicit_node_ip and len(masters) == 1 and not masters[0].node_ip:
        try:
            if ipaddress.ip_address(explicit_node_ip).version != 4:
                raise ValueError
        except ValueError:
            raise ValidationError("--node-ip 必须是 IPv4 地址")
        masters[0].node_ip = explicit_node_ip
    name = getattr(args, "name", None) or "k8s-%s" % (masters[0].address.replace(".", "-")[-18:] if masters else "cluster")
    name = safe_slug(name)
    spec = ClusterSpec(
        name=name,
        version=normalize_version(getattr(args, "version", "1.37")),
        masters=masters,
        workers=workers,
        vip=getattr(args, "vip", None),
        vip_interface=getattr(args, "vip_interface", None),
        vip_port=int(getattr(args, "vip_port", DEFAULT_VIP_PORT)),
        control_plane_endpoint=getattr(args, "control_plane_endpoint", None),
        external_endpoint=bool(getattr(args, "external_endpoint", False) or
                               getattr(args, "control_plane_endpoint", None)),
        pod_cidr=getattr(args, "pod_cidr", DEFAULT_POD_CIDR),
        service_cidr=getattr(args, "service_cidr", DEFAULT_SERVICE_CIDR),
        cni=getattr(args, "cni", DEFAULT_CNI),
        timezone=getattr(args, "timezone", DEFAULT_TIMEZONE),
        calico_version=getattr(args, "calico_version", DEFAULT_CALICO_VERSION),
        calico_image_repository=getattr(args, "calico_image_repository",
                                         DEFAULT_CALICO_IMAGE_REPOSITORY),
        calico_encapsulation=getattr(args, "calico_encapsulation",
                                      DEFAULT_CALICO_ENCAPSULATION),
        image_repository=getattr(args, "image_repository", DEFAULT_IMAGE_REPOSITORY),
        flannel_image=getattr(args, "flannel_image", DEFAULT_FLANNEL_IMAGE),
        flannel_cni_image=getattr(args, "flannel_cni_image", DEFAULT_FLANNEL_CNI_IMAGE),
        pod_iface=getattr(args, "pod_iface", None),
        package_repo_base=getattr(args, "package_repo_base", None),
        allow_upstream_fallback=not bool(getattr(args, "no_upstream_fallback", False)),
        allow_control_plane_without_ha=bool(getattr(args, "allow_control_plane_without_ha", False)),
        disable_swap=not bool(getattr(args, "keep_swap", False)),
        selinux_mode=getattr(args, "selinux", "permissive"),
        configure_firewall=not bool(getattr(args, "no_firewall", False)),
        node_port_range=getattr(args, "node_port_range", "30000-32767"),
    )
    ssh = SshOptions(
        user=getattr(args, "ssh_user", "root") or "root",
        port=int(getattr(args, "ssh_port", 22) or 22),
        key=getattr(args, "ssh_key", None),
        password=getattr(args, "ssh_password", None) or os.environ.get("KUBEDEPLOY_SSH_PASSWORD"),
        proxy_jump=getattr(args, "ssh_proxy_jump", None),
        strict_host_key_checking=getattr(args, "ssh_strict_host_key_checking", "accept-new"),
        known_hosts=getattr(args, "ssh_known_hosts", None),
        extra_options=list(getattr(args, "ssh_option", []) or []),
        connect_timeout=int(getattr(args, "ssh_connect_timeout", 20) or 20),
        become=not bool(getattr(args, "no_become", False)),
    )
    spec.ssh = ssh
    spec.validate()
    return spec
