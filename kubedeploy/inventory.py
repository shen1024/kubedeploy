"""节点清单构建、角色校验与展示。"""

from __future__ import annotations

from .errors import ValidationError
from .models import (ClusterSpec, NodeSpec, ROLE_MASTER, ROLE_WORKER,
                     parse_node_token)


def parse_role_groups(masters_raw, workers_raw):
    masters = []
    workers = []
    seen = set()
    seen_ip = set()
    for raw in masters_raw or []:
        node = parse_node_token(raw, ROLE_MASTER)
        if node.address in seen:
            raise ValidationError("节点 SSH 地址重复：%s" % node.address)
        if node.k8s_ip in seen_ip:
            raise ValidationError("节点 Kubernetes IP 重复：%s" % node.k8s_ip)
        seen.add(node.address); seen_ip.add(node.k8s_ip)
        masters.append(node)
    for raw in workers_raw or []:
        node = parse_node_token(raw, ROLE_WORKER)
        if node.address in seen:
            raise ValidationError("节点 %s 同时被指定为 master 和 worker" % node.address)
        if node.k8s_ip in seen_ip:
            raise ValidationError("节点 Kubernetes IP 重复：%s" % node.k8s_ip)
        seen.add(node.address); seen_ip.add(node.k8s_ip)
        workers.append(node)
    if not masters:
        raise ValidationError("至少需要一个 master 节点")
    return masters, workers


def node_role_counts(spec):
    return {
        "total": len(spec.masters) + len(spec.workers),
        "master": len(spec.masters),
        "worker": len(spec.workers),
    }


def describe_inventory(spec, facts_by_addr=None):
    facts_by_addr = facts_by_addr or {}
    lines = []
    lines.append("集群：%s    版本：%s    节点数：%d（master %d / worker %d）" % (
        spec.name, spec.version, spec.node_count, spec.master_count, spec.worker_count))
    lines.append("控制平面端点：%s%s" % (
        spec.endpoint_url,
        "（内部 VIP/HAProxy）" if spec.use_internal_ha else
        ("（外部负载均衡）" if spec.external_endpoint else "")))
    if spec.pod_cidr:
        lines.append("Pod CIDR：%s    Service CIDR：%s    CNI：%s" % (
            spec.pod_cidr, spec.service_cidr,
            "Calico %s/%s" % (spec.calico_version, spec.calico_encapsulation)
            if spec.cni == "calico" else "Flannel"))
    lines.append("")
    lines.append("%-4s %-18s %-18s %-8s %-10s %-14s %s" % (
        "#", "SSH 地址", "K8s 节点 IP", "角色", "架构", "系统", "主机名"))
    for idx, node in enumerate(spec.masters + spec.workers, 1):
        facts = facts_by_addr.get(node.address, {}) or {}
        arch = facts.get("arch", node.arch or "-")
        os_name = facts.get("os_name", node.os_id or "-")
        os_ver = facts.get("os_version", node.os_version or "")
        hostname = node.hostname or facts.get("hostname") or "-"
        if facts.get("hostname") and node.hostname and facts.get("hostname") != node.hostname:
            hostname = "%s (K8s:%s)" % (facts.get("hostname"), node.hostname)
        lines.append("%-4s %-18s %-18s %-8s %-10s %-14s %s" % (
            idx, node.address, node.k8s_ip, node.role, arch,
            (os_name + " " + os_ver).strip(), hostname))
    if any(not n.node_ip for n in spec.all_nodes):
        lines.append("")
        lines.append("提示：未显式指定 Kubernetes 私网 IP 的节点，连接后将自动探测 default route 网卡 IP；"
                     "公有云建议写成 公网IP=私网IP。")
    if spec.master_count > 1 and spec.master_count % 2 == 0:
        lines.append("")
        lines.append("警告：master 数量为偶数，堆叠 etcd 无法获得严格多数派容错，建议 1/3/5 台。")
    if spec.master_count > 1 and not spec.use_internal_ha and not spec.external_endpoint:
        lines.append("")
        lines.append("警告：控制平面端点不是 VIP/外部负载均衡，新增 master 不提供 API 故障切换。")
    return "\n".join(lines)


def expand_inventory(existing_spec, new_masters, new_workers):
    """在已有清单上追加节点，返回新的 ClusterSpec。"""
    existing_addrs = {n.address for n in existing_spec.all_nodes}
    existing_ips = {n.k8s_ip for n in existing_spec.all_nodes}
    masters = list(existing_spec.masters)
    workers = list(existing_spec.workers)
    for raw in new_masters or []:
        node = parse_node_token(raw, ROLE_MASTER)
        if node.address in existing_addrs:
            raise ValidationError("节点 SSH 地址 %s 已存在于集群中" % node.address)
        if node.k8s_ip in existing_ips:
            raise ValidationError("节点 Kubernetes IP %s 已存在于集群中" % node.k8s_ip)
        existing_addrs.add(node.address); existing_ips.add(node.k8s_ip)
        masters.append(node)
    for raw in new_workers or []:
        node = parse_node_token(raw, ROLE_WORKER)
        if node.address in existing_addrs:
            raise ValidationError("节点 SSH 地址 %s 已存在于集群中" % node.address)
        if node.k8s_ip in existing_ips:
            raise ValidationError("节点 Kubernetes IP %s 已存在于集群中" % node.k8s_ip)
        existing_addrs.add(node.address); existing_ips.add(node.k8s_ip)
        workers.append(node)
    if len(masters) <= len(existing_spec.masters) and len(workers) <= len(existing_spec.workers):
        raise ValidationError("没有新增节点")
    existing_spec.masters = masters
    existing_spec.workers = workers
    existing_spec.validate()
    return existing_spec
