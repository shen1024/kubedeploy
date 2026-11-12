"""部署编排：单节点、多节点、扩容。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, List

from .agent_client import NodeAgentClient
from .constants import (DEFAULT_FLANNEL_CNI_IMAGE, DEFAULT_FLANNEL_IMAGE,
                        DEFAULT_IMAGE_REPOSITORY, FALLBACK_IMAGE_REPOSITORY,
                        UPSTREAM_IMAGE_REPOSITORY, K8S_MINORS)
from .errors import AgentError, KubeDeployError, PreflightError, ValidationError
from .inventory import describe_inventory
from .models import (ClusterSpec, NodeSpec, ROLE_MASTER, ROLE_WORKER, SshOptions)
from .state import (BackupRecord, ClusterRecord,
                    install_default_kubeconfig)
from .util import (b64d, compare_versions, is_local_address, json_dumps,
                   parse_version_tuple, safe_slug, sanitize_node_name, utc_now)
from .versions import VersionRequest, parse_version_request


def _version_req_dict(version):
    req = parse_version_request(version)
    return {"minor": req.minor, "patch": req.patch}


def _image_repos(spec):
    repos = [spec.image_repository]
    if spec.image_repository != FALLBACK_IMAGE_REPOSITORY:
        repos.append(FALLBACK_IMAGE_REPOSITORY)
    if spec.allow_upstream_fallback:
        repos.append(UPSTREAM_IMAGE_REPOSITORY)
    result = []
    for item in repos:
        if item and item not in result:
            result.append(item)
    return result


class NodeRuntime:
    def __init__(self, node, client, console, auto_node_ip=True):
        self.node = node
        self.client = client
        self.console = console
        self.facts = {}
        self.hostname = ""
        self.node_ip = node.node_ip or node.address
        self.arch = ""
        self.auto_node_ip = auto_node_ip

    def _resolve_node_ip(self, facts):
        """SSH 地址可能是公有云 NAT/EIP；集群内应使用节点私网 IP。"""
        ips = [item for item in (facts.get("ips") or []) if item.get("ip")]
        ip_set = {item["ip"] for item in ips}
        explicit = self.node.node_ip
        if explicit:
            self.node_ip = explicit
            return
        if self.node.address in ip_set:
            self.node_ip = self.node.address
            self.node.node_ip = self.node_ip
            return
        if not self.auto_node_ip:
            self.node_ip = self.node.address
            return
        default_iface = facts.get("default_iface")
        chosen = ""
        for item in ips:
            if default_iface and item.get("interface") == default_iface:
                chosen = item["ip"]
                break
        if not chosen and ips:
            chosen = ips[0]["ip"]
        if chosen:
            self.node_ip = chosen
            self.node.node_ip = chosen
            self.console.warn(
                "%s：SSH 地址不是节点本地地址，自动使用默认网卡 IP %s 作为 Kubernetes 节点 IP"
                % (self.node.address, chosen))
        else:
            self.node_ip = self.node.address

    def prepare(self):
        self.console.step("连接并检查节点 %s ..." % self.node.address)
        self.client.prepare()
        facts = self.client.call("detect", {}, timeout=120, stream=False)
        self.facts = facts
        self.arch = facts.get("arch") or ""
        self.node.facts = facts
        self.node.arch = self.arch
        self.node.os_id = facts.get("os_id", "")
        self.node.os_version = facts.get("os_version", "")
        self.node.local = self.client.local
        self._resolve_node_ip(facts)
        # 已有节点的 API 名称优先；新节点使用真实主机名或自动规范化名称。
        preferred = (self.node.hostname or "").strip()
        raw_hostname = (facts.get("hostname") or "").strip()
        if preferred:
            self.hostname = preferred
        else:
            self.hostname = sanitize_node_name(raw_hostname, self.node_ip)
            if self.hostname != raw_hostname.lower():
                self.console.warn(
                    "%s：主机名 %r 不适合作为 Kubernetes 节点名，自动使用 %r"
                    % (self.node.address, raw_hostname or "(空)", self.hostname))
        self.node.hostname = self.hostname
        return facts

    def call(self, action, params=None, timeout=1800, stream=True):
        self.console.command(self.node.address, "%s" % action)
        return self.client.call(action, params or {}, timeout=timeout, stream=stream)

    def preflight(self, spec, mode="install", single_node=False, allow_untested=False):
        payload = {
            "version": spec.version,
            "role": self.node.role,
            "mode": mode,
            "node_ip": self.node_ip,
            "node_name": self.hostname,
            "cni": spec.cni,
            "calico_encapsulation": spec.calico_encapsulation,
            "vip_port": spec.vip_port,
            "ha": spec.use_internal_ha and self.node.role == ROLE_MASTER,
            "single_node": single_node,
            "allow_untested_os": allow_untested,
            "keep_swap": not spec.disable_swap,
        }
        return self.call("preflight", payload, timeout=180, stream=False)

    def close(self):
        self.client.close()


class Deployer:
    def __init__(self, spec, console, options):
        self.spec = spec
        self.console = console
        self.options = options
        self.nodes = {}
        self.records = {}
        self.exact_version = ""
        self.endpoint = ""
        self.init_data = {}
        self.pause_image = ""
        self.record = None
        self.all_runtimes = []

    # ---------- 基础 ----------
    def _build_runtimes(self):
        for node in self.spec.all_nodes:
            local = is_local_address(node.address) and not self.options.force_remote
            if local and os.name != "posix":
                raise ValidationError("本地部署仅支持 Linux；Windows 控制端请使用 --force-remote 或连接远程节点")
            client = NodeAgentClient(
                node.address, self.spec.ssh, self.console,
                local=local, force_upload=self.options.force_upload,
            )
            rt = NodeRuntime(node, client, self.console,
                             auto_node_ip=getattr(self.options, "auto_node_ip", True))
            self.nodes[node.address] = rt
            self.all_runtimes.append(rt)

    def _prepare_all(self):
        for rt in self.all_runtimes:
            rt.prepare()
        hostnames = {}
        k8s_ips = {}
        for rt in self.all_runtimes:
            if rt.hostname in hostnames:
                raise ValidationError("节点主机名重复：%s（%s 和 %s）" % (
                    rt.hostname, hostnames[rt.hostname], rt.node.address))
            hostnames[rt.hostname] = rt.node.address
            if rt.node_ip in k8s_ips:
                raise ValidationError("节点 Kubernetes IP 重复：%s（%s 和 %s）" % (
                    rt.node_ip, k8s_ips[rt.node_ip], rt.node.address))
            k8s_ips[rt.node_ip] = rt.node.address
            if rt.node.address in ("127.0.0.1", "::1", "localhost") or                     rt.node_ip in ("127.0.0.1", "::1", "localhost"):
                raise ValidationError("节点地址不能使用回环地址；请填写节点私网 IP")
        # 自动解析私网 IP 后重新校验 VIP/节点 IP 冲突和端点格式
        self.spec.validate()
        self.console.success("节点连接与基础探测完成（%d 台）" % len(self.all_runtimes))

    def _preflight_all(self, mode="install"):
        self.console.section("预检")
        all_errors = []
        single = self.spec.node_count == 1
        for rt in self.all_runtimes:
            data = rt.preflight(self.spec, mode=mode, single_node=single,
                                allow_untested=self.options.allow_untested_os)
            errors = data.get("errors") or []
            warnings = data.get("warnings") or []
            facts = data.get("facts") or rt.facts
            rt.facts = facts
            self.console.info("%s：%s %s / %s / cgroup v%s / 内核 %s" % (
                rt.node.address, facts.get("os_name", ""), facts.get("os_version", ""),
                facts.get("arch", ""), facts.get("cgroup_version", "?"),
                facts.get("kernel", "")))
            for warning in warnings:
                self.console.warn("%s：%s" % (rt.node.address, warning))
            if errors:
                for error in errors:
                    self.console.error("%s：%s" % (rt.node.address, error))
                all_errors.extend(["%s: %s" % (rt.node.address, e) for e in errors])
            else:
                self.console.success("%s：预检通过" % rt.node.address)
        if all_errors:
            raise PreflightError("节点预检未通过，已停止部署",
                                 hint="修复上方错误后重试；本工具不会在预检失败时修改节点")
        self.console.info(describe_inventory(self.spec, {k: v.facts for k, v in self.nodes.items()}))

    def _prepare_nodes(self):
        self.console.section("系统准备")
        for rt in self.all_runtimes:
            params = {
                "role": rt.node.role,
                "node_ip": rt.node_ip,
                "node_name": rt.hostname,
                "cni": self.spec.cni,
                "calico_encapsulation": self.spec.calico_encapsulation,
                "vip_port": self.spec.vip_port,
                "timezone": self.spec.timezone,
                "ha": self.spec.use_internal_ha and rt.node.role == ROLE_MASTER,
                "keep_swap": not self.spec.disable_swap,
                "selinux_mode": self.spec.selinux_mode,
                "configure_firewall": self.spec.configure_firewall,
                "node_port_range": self.spec.node_port_range,
            }
            data = rt.call("prepare", params, timeout=900, stream=True)
            for change in data.get("changes", []):
                self.console.debug("  %s: %s" % (rt.node.address, change))
            for warning in data.get("warnings", []):
                self.console.warn("%s：%s" % (rt.node.address, warning))
        self.console.success("系统参数、swap、SELinux、防火墙准备完成")

    def _install_packages(self):
        self.console.section("安装 Kubernetes 软件包")
        req = _version_req_dict(self.spec.version)
        exact = ""
        for rt in self.all_runtimes:
            payload = {
                "version_req": req,
                "repo_base": self.spec.package_repo_base,
                "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                "components": ["kubelet", "kubeadm", "kubectl"],
                "install_deps": True,
                "include_addons": True,
            }
            if exact:
                payload["exact_version"] = exact
            data = rt.call("install_packages", payload, timeout=2400, stream=True)
            self.console.info("%s：软件源 [%s] %s" % (
                rt.node.address, data.get("repo_source", "?"), data.get("repo_url", "?")))
            if data.get("fallback"):
                self.console.warn(
                    "Kubernetes 国内源未同步 %s，已回退官方源；如需严格国内源，"
                    "请使用 --no-upstream-fallback 或企业镜像" % data.get("k8s_version"))
            if not exact:
                exact = data.get("k8s_version")
            elif data.get("k8s_version") != exact:
                raise KubeDeployError("节点软件包版本不一致：%s vs %s" % (exact, data.get("k8s_version")))
            self.console.success("%s：Kubernetes %s 软件包就绪" % (rt.node.address, data.get("k8s_version")))
        self.exact_version = exact
        self.spec.version = exact
        self.console.info("目标版本解析为 %s" % exact)

    def _install_runtime(self):
        self.console.section("安装 containerd 运行时")
        for rt in self.all_runtimes:
            payload = {
                "arch": rt.arch,
                "containerd_version": self.options.containerd_version,
                "runc_version": self.options.runc_version,
                "containerd_sha256": getattr(self.options, "containerd_sha256", None),
                "runc_sha256": getattr(self.options, "runc_sha256", None),
                "force_runtime": False,
            }
            rt.call("install_runtime", payload, timeout=1800, stream=True)
            self.console.success("%s：containerd 就绪" % rt.node.address)

    # ---------- HA ----------
    def _ha_params(self, rt, first=False):
        return {
            "vip": self.spec.vip,
            "vip_interface": self.spec.vip_interface,
            "port": self.spec.vip_port,
            "masters": [x.k8s_ip for x in self.spec.masters],
            "node_ip": rt.node_ip,
            "is_first": first,
            "priority": 150 if first else 100,
            "cluster_name": self.spec.name,
            "vrrp_id": getattr(self, "vrrp_id", None),
            "vrrp_password": getattr(self, "vrrp_password", None),
        }

    def _setup_ha_first(self):
        if not self.spec.use_internal_ha:
            return
        self.console.section("配置控制平面高可用（HAProxy + Keepalived）")
        rt = self.nodes[self.spec.first_master.address]
        # 统一 VRID/密码，供后续 master 使用
        import hashlib, string
        text = self.spec.name
        self.vrrp_id = (int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 200) + 1
        alphabet = string.ascii_letters + string.digits
        digest = hashlib.sha256((self.spec.name + "|kubedeploy-vrrp").encode()).digest()
        self.vrrp_password = "".join(alphabet[b % len(alphabet)] for b in digest[:8])
        data = rt.call("setup_ha", self._ha_params(rt, first=True), timeout=900, stream=True)
        self.spec.vip_interface = data.get("interface")
        self._wait_vip([rt], timeout=45)
        self.console.success("VIP %s 已就绪（接口 %s）" % (self.spec.vip, self.spec.vip_interface))

    def _wait_vip(self, masters, timeout=60):
        deadline = time.time() + timeout
        last = []
        while time.time() < deadline:
            holders = []
            for rt in masters:
                try:
                    st = rt.call("ha_status", {"vip": self.spec.vip, "node_ip": rt.node_ip},
                                 timeout=60, stream=False)
                    if st.get("has_vip"):
                        holders.append(rt.node.address)
                    if not st.get("haproxy") or not st.get("keepalived"):
                        last.append("%s: haproxy=%s keepalived=%s" % (
                            rt.node.address, st.get("haproxy"), st.get("keepalived")))
                except Exception as exc:
                    last.append("%s: %s" % (rt.node.address, exc))
            if len(holders) > 1:
                raise KubeDeployError("检测到 VIP %s 同时出现在多个节点（%s），VRRP 脑裂风险" % (
                    self.spec.vip, ", ".join(holders)),
                    hint="检查 keepalived virtual_router_id/auth_pass 与网络隔离")
            if holders:
                return holders
            time.sleep(3)
        raise KubeDeployError("VIP 未在控制平面节点上出现（%s）" % ("; ".join(last[-5:]) or "超时"),
                              hint="检查 keepalived 日志、VRRP 112 端口与 VIP 是否被占用")

    def _setup_ha_node(self, rt):
        if not self.spec.use_internal_ha:
            return
        data = rt.call("setup_ha", self._ha_params(rt, first=False), timeout=900, stream=True)
        self.console.success("%s：HAProxy/Keepalived 就绪" % rt.node.address)

    # ---------- 集群 ----------
    def _init_first(self):
        self.console.section("初始化第一个控制平面节点")
        self.endpoint = self.spec.endpoint
        rt = self.nodes[self.spec.first_master.address]
        payload = {
            "version": self.exact_version,
            "endpoint": self.endpoint,
            "node_ip": rt.node_ip,
            "node_name": rt.hostname,
            "pod_cidr": self.spec.pod_cidr,
            "service_cidr": self.spec.service_cidr,
            "dns_domain": self.spec.dns_domain,
            "image_repositories": _image_repos(self.spec),
            "allow_upstream_fallback": self.spec.allow_upstream_fallback,
        }
        data = rt.call("init", payload, timeout=3600, stream=True)
        self.init_data = data
        self.pause_image = data.get("pause_image", "")
        self.spec.image_repository = data.get("image_repository", self.spec.image_repository)
        if not self.pause_image:
            raise KubeDeployError("初始化结果缺少 pause 镜像")
        admin_conf = b64d(data["admin_conf_b64"]).decode("utf-8")
        self.record = self._new_record()
        try:
            kubeconfig = self.record.save_kubeconfig(admin_conf)
            self.console.success("控制平面初始化完成，kubeconfig 已保存：%s" % kubeconfig)
            try:
                default_path, installed = install_default_kubeconfig(admin_conf)
                if installed:
                    self.console.success("默认 kubeconfig 已写入/更新：%s（kubectl 可直接使用）" % default_path)
                else:
                    self.console.info("已有默认 kubeconfig，未覆盖：%s" % default_path)
            except Exception as exc:
                self.console.warn("默认 kubeconfig 写入失败：%s" % exc)
            self.console.info("使用：export KUBECONFIG=%s" % kubeconfig)
        except Exception as exc:
            self.console.warn("本地 kubeconfig 保存失败：%s；可从控制平面 /etc/kubernetes/admin.conf 获取" % exc)
        # 尽早落盘，后续 join/CNI 失败时仍可通过已有集群功能接管
        self._save_state()

    def _new_record(self):
        rec = ClusterRecord(
            name=self.spec.name,
            version=self.exact_version or self.spec.version,
            endpoint=self.spec.endpoint,
            spec=self.spec.to_dict(),
            node_count=self.spec.node_count,
            master_count=self.spec.master_count,
            worker_count=self.spec.worker_count,
            facts={k: v.facts for k, v in self.nodes.items()},
        )
        rec.add_history("created", "部署 %d 节点集群" % self.spec.node_count)
        return rec

    def _generate_join(self):
        rt = self.nodes[self.spec.first_master.address]
        data = rt.call("generate_join", {"control_plane": True}, timeout=300, stream=True)
        return data

    def _join_nodes(self):
        masters = [x for x in self.spec.masters[1:]]
        workers = list(self.spec.workers)
        if not masters and not workers:
            return
        self.console.section("加入节点")
        join = self._generate_join()
        for node in masters:
            rt = self.nodes[node.address]
            payload = {
                "version": self.exact_version,
                "endpoint": self.spec.endpoint,
                "node_ip": rt.node_ip,
                "node_name": rt.hostname,
                "control_plane": True,
                "token": join.get("token"),
                "ca_hash": join.get("ca_hash"),
                "certificate_key": join.get("certificate_key"),
                "pause_image": self.pause_image,
                "image_repositories": _image_repos(self.spec),
                "allow_upstream_fallback": self.spec.allow_upstream_fallback,
            }
            rt.call("join", payload, timeout=2400, stream=True)
            self.console.success("%s：已加入控制平面" % rt.node.address)
            self._setup_ha_node(rt)
            self._wait_vip([self.nodes[x.address] for x in self.spec.masters if x.address in self.nodes], timeout=45)
        for node in workers:
            rt = self.nodes[node.address]
            payload = {
                "version": self.exact_version,
                "endpoint": self.spec.endpoint,
                "node_ip": rt.node_ip,
                "node_name": rt.hostname,
                "control_plane": False,
                "token": join.get("token"),
                "ca_hash": join.get("ca_hash"),
                "pause_image": self.pause_image,
                "image_repositories": _image_repos(self.spec),
                "allow_upstream_fallback": self.spec.allow_upstream_fallback,
            }
            rt.call("join", payload, timeout=1800, stream=True)
            self.console.success("%s：已加入 worker" % rt.node.address)

    def _deploy_cni(self):
        label = "Calico" if self.spec.cni == "calico" else "Flannel"
        self.console.section("部署 %s CNI" % label)
        payload = {
            "cni": self.spec.cni,
            "pod_cidr": self.spec.pod_cidr,
            "calico_version": self.spec.calico_version,
            "calico_image_repository": self.spec.calico_image_repository,
            "calico_encapsulation": self.spec.calico_encapsulation,
            "flannel_image": self.spec.flannel_image,
            "flannel_cni_image": self.spec.flannel_cni_image,
            "flannel_iface": self.spec.pod_iface,
        }
        for rt in self.all_runtimes:
            rt.call("preload_cni", payload, timeout=1800, stream=True)
        first = self.nodes[self.spec.first_master.address]
        first.call("deploy_cni", payload, timeout=1200, stream=True)
        self.console.success("%s CNI 已部署" % label)

    def _wait_ready(self):
        self.console.section("等待节点 Ready")
        first = self.nodes[self.spec.first_master.address]
        names = [self.nodes[x.address].hostname for x in self.spec.all_nodes]
        data = first.call("wait_ready", {"node_names": names, "timeout": 600},
                          timeout=660, stream=True)
        self.console.success("节点已 Ready：%s" % ", ".join(data.get("ready", [])))

    def _untaint_single(self):
        if self.spec.node_count != 1:
            return
        rt = self.all_runtimes[0]
        rt.call("untaint_master", {"node_name": rt.hostname}, timeout=120, stream=True)
        self.console.info("单节点集群已移除控制平面 NoSchedule 污点")

    def _wait_coredns(self):
        self.console.section("等待 CoreDNS")
        first = self.nodes[self.spec.first_master.address]
        first.call("wait_coredns", {"timeout": 300}, timeout=360, stream=True)
        self.console.success("CoreDNS 已就绪")

    def _save_state(self):
        if self.record is None:
            return
        self.record.version = self.exact_version or self.spec.version
        self.record.endpoint = self.spec.endpoint
        self.record.spec = self.spec.to_dict()
        self.record.node_count = self.spec.node_count
        self.record.master_count = self.spec.master_count
        self.record.worker_count = self.spec.worker_count
        self.record.facts = {k: v.facts for k, v in self.nodes.items()}
        path = None
        try:
            path = self.record.save()
        except Exception as exc:
            self.console.warn("本地集群状态写入失败：%s（可设置 KUBEDEPLOY_HOME）" % exc)
        try:
            first = self.nodes[self.spec.first_master.address]
            first.call("write_state", {"state": self._state_dict()}, timeout=120, stream=False)
        except Exception as exc:
            self.console.warn("写入节点状态失败：%s" % exc)
        if path:
            self.console.info("本地集群状态：%s" % path)

    def _state_dict(self):
        state = self.record.to_dict() if hasattr(self.record, "to_dict") else {}
        # ClusterRecord 没有 to_dict，手工构造
        import dataclasses
        return dataclasses.asdict(self.record)

    def _verify(self):
        self.console.section("集群验证")
        first = self.nodes[self.spec.first_master.address]
        info = first.call("cluster_info", {}, timeout=180, stream=True)
        nodes = info.get("nodes", [])
        if not nodes:
            raise KubeDeployError("无法读取集群节点列表")
        rows = [[n.get("name", ""), n.get("role", ""), n.get("ip", ""),
                 n.get("version", ""), "Ready" if n.get("ready") else "NotReady"] for n in nodes]
        self.console.table(rows, headers=["节点", "角色", "IP", "版本", "状态"])
        if len(nodes) != self.spec.node_count:
            self.console.warn("API 中节点数为 %d，期望 %d；请检查 gzip/重复主机名" % (
                len(nodes), self.spec.node_count))
        not_ready = [n["name"] for n in nodes if not n.get("ready")]
        if not_ready:
            raise KubeDeployError("节点未 Ready：%s" % ", ".join(not_ready))
        self.console.success("集群验证通过（%d 节点，版本 %s）" % (
            len(nodes), info.get("version", self.exact_version)))

    # ---------- 入口 ----------
    def _existing_cluster_guard(self, apply=True):
        """检测到已有健康单节点集群时跳过重复安装，避免 8472 端口误报。"""
        existing = []
        for rt in self.all_runtimes:
            facts = rt.facts or {}
            current = facts.get("existing_cluster") or {}
            if current.get("installed") or facts.get("kubelet_active"):
                existing.append(rt)
        if not existing:
            return False
        if self.spec.node_count != 1 or len(existing) != 1:
            raise PreflightError(
                "检测到节点上已有 Kubernetes 集群，不能作为新集群重复初始化",
                hint="已有集群请用 status/expand/upgrade；确认要重装时先执行 reset")
        rt = existing[0]
        self.console.warn("%s：检测到已有 Kubernetes 配置，正在确认真实状态 ..." % rt.node.address)
        info = rt.call("cluster_info", {}, timeout=180, stream=False)
        nodes = info.get("nodes") or []
        if not nodes:
            raise PreflightError(
                "节点上存在 Kubernetes 配置，但 API 不可用，不能直接重复安装",
                hint="先检查 kubelet/API 日志；确认要重装时执行 reset，避免覆盖现有数据")
        if len(nodes) != 1:
            raise PreflightError(
                "节点上已有 %d 个节点的集群，不能用单节点模式覆盖" % len(nodes),
                hint="请使用 status/expand/upgrade，或先 reset 后再重装")
        version = info.get("version") or self.spec.version
        if version != self.spec.version:
            self.console.warn("现有集群版本为 %s，请求版本为 %s；本次不执行重复安装"
                              % (version, self.spec.version))
        self.console.warn("现有集群可用，跳过重复安装；如需重装请先执行 reset")
        self.exact_version = version
        self.spec.version = version
        self.spec.pod_cidr = info.get("pod_cidr") or self.spec.pod_cidr
        self.spec.service_cidr = info.get("service_cidr") or self.spec.service_cidr
        self.spec.image_repository = info.get("image_repository") or self.spec.image_repository
        self.record = self._new_record()
        self.record.history = []
        self.record.add_history("adopted", "检测到已有单节点集群，跳过重复安装")
        self.console.table(
            [[n.get("name", ""), n.get("role", ""), n.get("ip", ""),
              n.get("version", ""), "Ready" if n.get("ready") else "NotReady"] for n in nodes],
            headers=["节点", "角色", "IP", "版本", "状态"])
        content = b64d(info["admin_conf_b64"]).decode("utf-8") if info.get("admin_conf_b64") else ""
        if apply and content:
            try:
                path = self.record.save_kubeconfig(content)
                self.console.success("kubeconfig 已刷新：%s" % path)
                default_path, installed = install_default_kubeconfig(content)
                if installed:
                    self.console.success("默认 kubeconfig 已写入/更新：%s" % default_path)
            except Exception as exc:
                self.console.warn("kubeconfig 刷新失败：%s" % exc)
        if apply:
            try:
                path = self.record.save()
                self.console.info("本地集群状态：%s" % path)
            except Exception as exc:
                self.console.warn("本地集群状态写入失败：%s" % exc)
        ready = all(bool(n.get("ready")) for n in nodes)
        if apply and (not ready or not info.get("cni")):
            self.console.warn("现有控制平面已就绪，但 CNI/节点尚未 Ready；继续补装 CNI")
            self._deploy_cni()
            self._wait_ready()
            self._untaint_single()
            self._wait_coredns()
            self._verify()
            self._save_state()
            self.console.success("CNI 补装完成")
            return True
        self.console.success("现有集群可直接使用；未修改节点")
        return True

    def install(self):
        if self.options.dry_run:
            self.console.info("目标版本：%s；端点：%s" % (self.spec.version, self.spec.endpoint))
            self.console.info("[dry-run] 不建立 SSH 连接、不修改任何节点")
            return None
        self._build_runtimes()
        try:
            self._prepare_all()
            if self._existing_cluster_guard():
                return self.record
            self._preflight_all("install")
            if not self.options.preflight_only:
                self._prepare_nodes()
                self._install_packages()
                self._install_runtime()
                self._setup_ha_first()
                self._init_first()
                self._join_nodes()
                self._deploy_cni()
                self._wait_ready()
                self._untaint_single()
                self._wait_coredns()
                self._verify()
                self._save_state()
                self.console.section("完成")
                self.console.success("Kubernetes %s 集群部署完成" % self.exact_version)
                if self.spec.use_internal_ha:
                    self.console.info("API 端点：%s（VIP %s）" % (self.spec.endpoint_url, self.spec.vip))
                else:
                    self.console.info("API 端点：%s" % self.spec.endpoint_url)
                if self.record:
                    self.console.info("kubeconfig：%s" % self.record.kubeconfig_path())
                return self.record
        finally:
            for rt in self.all_runtimes:
                rt.close()

    def preflight(self):
        if self.options.dry_run:
            self.console.info("[dry-run] 仅预检；不建立 SSH 连接")
            return None
        self._build_runtimes()
        try:
            self._prepare_all()
            if self._existing_cluster_guard(apply=False):
                return None
            self._preflight_all("install")
            if self.options.preflight_only:
                self.console.success("仅预检完成，未修改节点")
        finally:
            for rt in self.all_runtimes:
                rt.close()
