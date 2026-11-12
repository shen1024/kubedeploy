"""现有集群操作：扩容、滚动升级、基于备份的回滚。"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import string
import time

from .agent_client import NodeAgentClient
from .constants import (DEFAULT_FLANNEL_CNI_IMAGE, DEFAULT_FLANNEL_IMAGE,
                        DEFAULT_IMAGE_REPOSITORY, UPSTREAM_IMAGE_REPOSITORY)
from .errors import AgentError, KubeDeployError, PreflightError, ValidationError
from .inventory import expand_inventory
from .models import (ClusterSpec, NodeSpec, ROLE_MASTER, ROLE_WORKER,
                     parse_node_token)
from .orchestrator import NodeRuntime, _image_repos, _version_req_dict
from .state import (ClusterRecord, install_default_kubeconfig,
                    new_backup_name)
from .util import b64d, b64e, compare_versions, is_local_address, parse_version_tuple, utc_now
from .versions import parse_version_request, VersionRequest


def _endpoint_parts(endpoint):
    endpoint = re.sub(r"^https?://", "", endpoint or "")
    if endpoint.startswith("["):
        host = endpoint[1:].split("]")[0]
        rest = endpoint.split("]", 1)[1]
        port = rest[1:] if rest.startswith(":") else "6443"
        return host, int(port or 6443)
    if ":" in endpoint:
        host, port = endpoint.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return endpoint, 6443
    return endpoint, 6443


class ExistingCluster:
    def __init__(self, bootstrap, ssh_options, console, options):
        self.bootstrap = bootstrap
        self.ssh = ssh_options
        self.console = console
        self.options = options
        self.local = is_local_address(bootstrap) and not getattr(options, "force_remote", False)
        self.bootstrap_client = NodeAgentClient(
            bootstrap, ssh_options, console, local=self.local,
            force_upload=getattr(options, "force_upload", False))
        self.bootstrap_client.prepare()
        self.info = self.bootstrap_client.call("cluster_info", {}, timeout=240, stream=False)
        if not self.info.get("kubeconfig_exists"):
            raise KubeDeployError("%s 不是可用的控制平面节点（未找到 admin.conf）" % bootstrap)
        state_resp = self.bootstrap_client.call("read_state", {}, timeout=60, stream=False)
        self.remote_state = state_resp.get("state") if state_resp.get("exists") else None
        self.nodes_info = self.info.get("nodes") or []
        if not self.nodes_info:
            raise KubeDeployError("无法从集群 API 读取节点列表")
        self.current_version = self._clean_version(
            self.info.get("version") or (self.remote_state or {}).get("version"))
        if not self.current_version:
            raise KubeDeployError("无法识别当前 Kubernetes 版本")
        self.spec = self._build_spec()
        self.record = self._build_record()
        self.runtimes = {}

    @staticmethod
    def _clean_version(value):
        if not value:
            return ""
        m = re.search(r"v?(\d+\.\d+\.\d+)", str(value))
        return "v" + m.group(1) if m else ""

    def _build_spec(self):
        state_spec = None
        if self.remote_state and self.remote_state.get("spec"):
            try:
                state_spec = ClusterSpec.from_dict(self.remote_state["spec"])
            except Exception as exc:
                self.console.warn("远程状态中的集群清单无法解析，将根据 API 重建：%s" % exc)
        node_ip_by_name = {}
        masters, workers = [], []
        state_nodes = state_spec.all_nodes if state_spec else []
        for item in self.nodes_info:
            name = item.get("name", "")
            ip = item.get("ip") or ""
            if not ip:
                if state_spec:
                    for existing in state_nodes:
                        if existing.hostname == name:
                            ip = existing.k8s_ip
            if not ip:
                raise KubeDeployError("节点 %s 没有 InternalIP，无法继续" % name)
            node_ip_by_name[name] = ip
            # 恢复 SSH 地址与 K8s 私网 IP 的映射；公有云场景下二者不同。
            existing_node = None
            for candidate in state_nodes:
                if (candidate.hostname == name or candidate.k8s_ip == ip
                        or candidate.address == ip):
                    existing_node = candidate
                    break
            ssh_address = existing_node.address if existing_node else ip
            if item.get("role") == "master":
                masters.append(NodeSpec(address=ssh_address, role=ROLE_MASTER,
                                        hostname=name, node_ip=ip))
            else:
                workers.append(NodeSpec(address=ssh_address, role=ROLE_WORKER,
                                        hostname=name, node_ip=ip))
        if not masters:
            raise KubeDeployError("集群 API 中没有控制平面节点")
        # 保留原清单中的网络/镜像等设置
        if state_spec:
            spec = state_spec
            spec.masters = masters
            spec.workers = workers
            spec.ssh = self.ssh
        else:
            spec = ClusterSpec(
                name="k8s-%s" % masters[0].address.replace(".", "-"),
                version=self.current_version,
                masters=masters, workers=workers,
                image_repository=DEFAULT_IMAGE_REPOSITORY,
                ssh=self.ssh,
            )
        spec.version = self.current_version
        endpoint = self.info.get("endpoint") or ""
        if not endpoint:
            server = self.info.get("server") or ""
            endpoint = re.sub(r"^https?://", "", server)
        if endpoint:
            host, port = _endpoint_parts(endpoint)
            node_ips = {n.k8s_ip for n in masters + workers}
            if state_spec and state_spec.vip and host == state_spec.vip:
                spec.vip = state_spec.vip
                spec.vip_interface = state_spec.vip_interface
                spec.vip_port = port
                spec.control_plane_endpoint = None
                spec.external_endpoint = False
            elif host in node_ips:
                # 使用首个 master 作为端点；多 master 时显式记录，单 master 依赖 endpoint 属性
                spec.control_plane_endpoint = ("%s:%d" % (host, port)) if len(masters) > 1 else None
                spec.vip = None
                spec.external_endpoint = False
            else:
                spec.control_plane_endpoint = "%s:%d" % (host, port)
                spec.external_endpoint = True
                spec.vip = None
        spec.pod_cidr = self.info.get("pod_cidr") or spec.pod_cidr
        spec.service_cidr = self.info.get("service_cidr") or spec.service_cidr
        if self.info.get("image_repository"):
            spec.image_repository = self.info["image_repository"]
        if self.info.get("cni"):
            spec.cni = self.info["cni"]
        spec.allow_control_plane_without_ha = True
        spec.allow_upstream_fallback = not getattr(self.options, "no_upstream_fallback", False)
        return spec

    def _build_record(self):
        if self.remote_state:
            data = dict(self.remote_state)
            data = {k: v for k, v in data.items() if k in ClusterRecord.__dataclass_fields__}
            try:
                record = ClusterRecord(**data)
            except Exception:
                record = None
            if record:
                record.spec = self.spec.to_dict()
                record.version = self.current_version
                record.backups = record.backups or []
                record.history = record.history or []
                return record
        record = ClusterRecord(
            name=self.spec.name, version=self.current_version,
            endpoint=self.spec.endpoint, spec=self.spec.to_dict(),
            node_count=self.spec.node_count, master_count=self.spec.master_count,
            worker_count=self.spec.worker_count,
        )
        record.add_history("discovered", "接管已有集群")
        return record

    def build_runtimes(self, only_addresses=None):
        wanted = set(only_addresses) if only_addresses else {n.address for n in self.spec.all_nodes}
        for node in self.spec.all_nodes:
            if node.address not in wanted or node.address in self.runtimes:
                continue
            local = node.address == self.bootstrap and self.local
            client = (self.bootstrap_client if local else NodeAgentClient(
                node.address, self.ssh, self.console, local=local,
                force_upload=getattr(self.options, "force_upload", False)))
            rt = NodeRuntime(node, client, self.console,
                             auto_node_ip=getattr(self.options, "auto_node_ip",
                                                  not getattr(self.options, "no_auto_node_ip", False)))
            rt.prepare()
            self.runtimes[node.address] = rt
        return self.runtimes

    def master_runtimes(self):
        return [self.runtimes[n.address] for n in self.spec.masters]

    def all_runtimes(self):
        return [self.runtimes[n.address] for n in self.spec.all_nodes]

    def first_rt(self):
        return self.runtimes[self.spec.first_master.address]

    def save_state(self):
        self.record.version = self.current_version
        self.record.endpoint = self.spec.endpoint
        self.record.spec = self.spec.to_dict()
        self.record.node_count = self.spec.node_count
        self.record.master_count = self.spec.master_count
        self.record.worker_count = self.spec.worker_count
        self.record.facts = {k: rt.facts for k, rt in self.runtimes.items()}
        try:
            self.record.save()
        except Exception as exc:
            self.console.warn("本地集群状态写入失败：%s（可设置 KUBEDEPLOY_HOME）" % exc)
        try:
            self.first_rt().call("write_state", {"state": dataclasses.asdict(self.record)},
                                 timeout=120, stream=False)
        except Exception as exc:
            self.console.warn("保存节点侧状态失败：%s" % exc)

    def refresh_kubeconfig(self):
        info = self.first_rt().call("cluster_info", {}, timeout=180, stream=False)
        if info.get("admin_conf_b64"):
            try:
                content = b64d(info["admin_conf_b64"]).decode("utf-8")
                path = self.record.save_kubeconfig(content)
                self.console.info("kubeconfig 已更新：%s" % path)
                try:
                    default_path, installed = install_default_kubeconfig(content)
                    if installed:
                        self.console.success("默认 kubeconfig 已写入/更新：%s" % default_path)
                except Exception as exc:
                    self.console.warn("默认 kubeconfig 写入失败：%s" % exc)
                return path
            except Exception as exc:
                self.console.warn("本地 kubeconfig 更新失败：%s；可从控制平面重新获取" % exc)
        return None

    def close(self):
        for rt in self.runtimes.values():
            rt.close()
        self.bootstrap_client.close()

    # ---------- 扩容 ----------
    def expand(self, new_masters, new_workers):
        new_master_nodes = [parse_node_token(x, ROLE_MASTER) for x in (new_masters or [])]
        new_worker_nodes = [parse_node_token(x, ROLE_WORKER) for x in (new_workers or [])]
        all_new = [n.address for n in new_master_nodes + new_worker_nodes]
        expand_inventory(self.spec, new_masters, new_workers)
        self.spec.version = self.current_version
        # 单节点集群原来用首 master IP 作端点；扩容为多 master 时显式固定该端点。
        if (self.spec.master_count > 1 and not self.spec.vip and
                not self.spec.external_endpoint and not self.spec.control_plane_endpoint):
            self.spec.control_plane_endpoint = "%s:6443" % self.spec.first_master.k8s_ip
        self.spec.validate()
        self.console.info("扩容后节点总数：%d（master %d / worker %d）" % (
            self.spec.node_count, self.spec.master_count, self.spec.worker_count))
        # 至少准备 bootstrap/现有 master 的 agent，供 generate_join、CNI 和 HA 刷新使用
        prepare_addresses = list(dict.fromkeys(
            list(all_new) + [m.address for m in self.spec.masters]))
        self.build_runtimes(only_addresses=prepare_addresses)
        new_rts = [self.runtimes[a] for a in all_new]
        try:
            for rt in new_rts:
                rt.prepare()
            self._preflight(new_rts, "expand")
            for rt in new_rts:
                params = {
                    "role": rt.node.role, "node_ip": rt.node_ip,
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
                rt.call("prepare", params, timeout=900, stream=True)
            for rt in new_rts:
                rt.call("install_packages", {
                    "version_req": _version_req_dict(self.current_version),
                    "exact_version": self.current_version,
                    "repo_base": self.spec.package_repo_base,
                    "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                    "components": ["kubelet", "kubeadm", "kubectl"],
                    "install_deps": True,
                    "include_addons": True,
                }, timeout=2400, stream=True)
                rt.call("install_runtime", {
                    "arch": rt.arch,
                    "containerd_version": getattr(self.options, "containerd_version", None),
                    "runc_version": getattr(self.options, "runc_version", None),
                    "containerd_sha256": getattr(self.options, "containerd_sha256", None),
                    "runc_sha256": getattr(self.options, "runc_sha256", None),
                }, timeout=1800, stream=True)
            if any(n.role == ROLE_MASTER for n in self.spec.masters if n.address in all_new):
                if self.spec.external_endpoint:
                    self.console.warn(
                        "新 master 已加入集群；请在企业/云负载均衡中把该 master 私网 IP:6443 "
                        "加入后端目标组并确认健康检查")
                if not self.spec.use_internal_ha and not self.spec.external_endpoint:
                    if not self.spec.allow_control_plane_without_ha:
                        raise ValidationError(
                            "原集群未使用 VIP/外部负载均衡端点，无法安全地增加 master",
                            hint="请只增加 worker，或在原集群上先完成端点/HA 迁移",
                        )
                    self.console.warn("原集群未配置 VIP/外部 LB；新增控制平面不具备 API 负载均衡/故障切换")
            join = self.first_rt().call("generate_join", {"control_plane": True},
                                       timeout=300, stream=True)
            pause = self.info.get("pause_image") or ""
            for rt in [self.runtimes[n.address] for n in self.spec.masters if n.address in all_new]:
                rt.call("join", {
                    "version": self.current_version, "endpoint": self.spec.endpoint,
                    "node_ip": rt.node_ip, "node_name": rt.hostname,
                    "control_plane": True, "token": join["token"],
                    "ca_hash": join["ca_hash"], "certificate_key": join["certificate_key"],
                    "pause_image": pause,
                    "image_repositories": _image_repos(self.spec),
                    "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                }, timeout=2400, stream=True)
                self.console.success("%s：已加入控制平面" % rt.node.address)
                if self.spec.use_internal_ha:
                    self._setup_ha(rt, first=False)
            if self.spec.use_internal_ha:
                # 扩容 master 后刷新所有既有 master 的 HAProxy 后端和 Keepalived peer 列表
                self.build_runtimes(only_addresses=[m.address for m in self.spec.masters])
                for master in self.spec.masters:
                    self._setup_ha(self.runtimes[master.address],
                                   first=(master.address == self.spec.first_master.address))
                masters_rt = [self.runtimes[m.address] for m in self.spec.masters]
                self._wait_vip(masters_rt, timeout=90)
                self.console.success("所有控制平面已刷新 HA 配置，VIP 正常")
            for rt in [self.runtimes[n.address] for n in self.spec.workers if n.address in all_new]:
                rt.call("join", {
                    "version": self.current_version, "endpoint": self.spec.endpoint,
                    "node_ip": rt.node_ip, "node_name": rt.hostname,
                    "control_plane": False, "token": join["token"],
                    "ca_hash": join["ca_hash"], "pause_image": pause,
                    "image_repositories": _image_repos(self.spec),
                    "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                }, timeout=1800, stream=True)
                self.console.success("%s：已加入 worker" % rt.node.address)
            for rt in new_rts:
                rt.call("preload_cni", {"flannel_image": self.spec.flannel_image,
                                        "flannel_cni_image": self.spec.flannel_cni_image},
                        timeout=1200, stream=True)
            self.first_rt().call("deploy_cni", {
                "pod_cidr": self.spec.pod_cidr,
                "flannel_image": self.spec.flannel_image,
                "flannel_cni_image": self.spec.flannel_cni_image,
                "flannel_iface": self.spec.pod_iface,
            }, timeout=900, stream=True)
            names = [self.runtimes[n.address].hostname if n.address in self.runtimes
                     else (n.hostname or "") for n in self.spec.all_nodes]
            names = [x for x in names if x]
            self.first_rt().call("wait_ready", {"node_names": names, "timeout": 600},
                                 timeout=660, stream=True)
            self.record.add_history("expand", "新增节点 %s" % ", ".join(all_new))
            self.record.spec = self.spec.to_dict()
            self.save_state()
            self.refresh_kubeconfig()
            self.console.success("扩容完成：当前 %d 节点" % self.spec.node_count)
        finally:
            for rt in new_rts:
                rt.close()

    def _wait_vip(self, masters, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            holders = []
            for rt in masters:
                try:
                    data = rt.call("ha_status", {"vip": self.spec.vip, "node_ip": rt.node_ip},
                                   timeout=30, stream=False)
                    if data.get("has_vip"):
                        holders.append(rt.node.address)
                except Exception:
                    pass
            if len(holders) > 1:
                raise KubeDeployError("检测到 VIP 同时出现在多个节点：%s" % ", ".join(holders),
                                      hint="检查 keepalived 的 VRID、auth_pass 与网络隔离")
            if holders:
                return holders[0]
            time.sleep(3)
        raise KubeDeployError("扩容后 VIP 未在控制平面节点上出现",
                              hint="检查 keepalived/haproxy 日志和 VRRP 网络")

    def _setup_ha(self, rt, first=False):
        import hashlib
        text = self.spec.name
        vrid = (int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 200) + 1
        alphabet = string.ascii_letters + string.digits
        digest = hashlib.sha256((self.spec.name + "|kubedeploy-vrrp").encode()).digest()
        password = "".join(alphabet[b % len(alphabet)] for b in digest[:8])
        data = rt.call("setup_ha", {
            "vip": self.spec.vip, "vip_interface": self.spec.vip_interface,
            "port": self.spec.vip_port,
            "masters": [x.k8s_ip for x in self.spec.masters],
            "node_ip": rt.node_ip, "is_first": first,
            "priority": 150 if first else 100,
            "cluster_name": self.spec.name,
            "vrrp_id": vrid, "vrrp_password": password,
        }, timeout=900, stream=True)
        if not self.spec.vip_interface:
            self.spec.vip_interface = data.get("interface")
        self.console.success("%s：HAProxy/Keepalived 就绪" % rt.node.address)

    # ---------- 升级 ----------
    def upgrade(self, target):
        cur = parse_version_tuple(self.current_version)
        tgt = parse_version_tuple(target)
        if not cur or not tgt:
            raise ValidationError("版本格式无效：%s -> %s" % (self.current_version, target))
        if tgt[1] <= cur[1]:
            raise ValidationError("目标版本 %s 必须高于当前版本 %s" % (target, self.current_version))
        if tgt[1] >= 35:
            facts = ((getattr(self, "remote_state", None) or {}).get("facts") or {})
            if not facts and self.record:
                facts = self.record.facts or {}
            for addr, fact in facts.items():
                if isinstance(fact, dict) and str(fact.get("cgroup_version")) == "1":
                    raise ValidationError(
                        "目标 Kubernetes %s 需要 cgroup v2，但节点 %s 当前是 cgroup v1"
                        % (target, addr),
                        hint="RHEL/CentOS 8、默认 Ubuntu 20.04 等 cgroup v1 系统无法运行 "
                             "Kubernetes 1.35+；请迁移到 RHEL 9/10、Ubuntu 22.04+ 的 cgroup v2 "
                             "节点重建 1.37 集群并迁移工作负载。不要尝试 --force 绕过。")
        if tgt[1] - cur[1] > 1:
            steps = ["1.%d" % m for m in range(cur[1] + 1, tgt[1] + 1)]
            raise ValidationError(
                "不支持跨多个次版本升级：%s -> %s" % (self.current_version, target),
                hint="请按 %s 逐次执行 upgrade；每次完成并验证后再升级下一个 minor"
                     % " -> ".join(steps))
        # 版本路径和 cgroup 通过后再连接节点/查询目标包
        self.build_runtimes()
        req = _version_req_dict(target)
        probe = self.first_rt().call("check_packages", {
            "version_req": req,
            "repo_base": self.spec.package_repo_base,
            "allow_upstream_fallback": self.spec.allow_upstream_fallback,
            "components": ["kubelet", "kubeadm", "kubectl"],
        }, timeout=600, stream=True)
        exact = probe["k8s_version"]
        if probe.get("fallback"):
            self.console.warn("目标版本国内软件源未同步，已回退官方源；请确认网络/合规策略")
        cur_minor = parse_version_tuple(self.current_version)[1]
        tgt_minor = parse_version_tuple(exact)[1]
        if compare_versions(exact, self.current_version) <= 0:
            raise ValidationError("目标版本 %s 必须高于当前版本 %s" % (exact, self.current_version))
        if tgt_minor - cur_minor > 1:
            raise ValidationError("不支持跨多个次版本升级：%s -> %s" % (self.current_version, exact),
                                  hint="Kubernetes 官方要求逐次升级一个 minor；请完成本次后再执行下一次")
        self.spec.version = exact
        try:
            self._preflight(self.all_runtimes(), "upgrade")
            # 在改动任何节点前确认所有节点都能获取目标版本包
            for rt in self.all_runtimes():
                probe_node = rt.call("check_packages", {
                    "version_req": _version_req_dict(exact),
                    "exact_version": exact,
                    "repo_base": self.spec.package_repo_base,
                    "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                    "components": ["kubelet", "kubeadm", "kubectl"],
                }, timeout=600, stream=False)
                if probe_node.get("k8s_version") != exact:
                    raise KubeDeployError("节点 %s 无法解析目标版本 %s" % (rt.node.address, exact))
            backup_id = new_backup_name(self.current_version, exact)
            backup_nodes = []
            if not getattr(self.options, "skip_backup", False):
                for rt in self.master_runtimes():
                    self.console.step("备份控制平面 %s ..." % rt.node.address)
                    meta = rt.call("backup", {
                        "backup_id": backup_id,
                        "current_version": self.current_version,
                        "target_version": exact,
                        "node_name": rt.hostname,
                        "node_ip": rt.node_ip,
                    }, timeout=1200, stream=True)
                    backup_nodes.append({
                        "address": rt.node.address, "node_name": rt.hostname,
                        "snapshot_path": meta["snapshot_path"],
                        "snapshot_sha256": meta["snapshot_sha256"],
                        "archive_path": meta["archive_path"],
                        "archive_sha256": meta["archive_sha256"],
                    })
                self.record.add_backup({
                    "backup_id": backup_id,
                    "from_version": self.current_version,
                    "to_version": exact,
                    "created_at": utc_now(),
                    "nodes": backup_nodes,
                })
                self.save_state()
                self.console.success("控制平面备份完成：%s" % backup_id)
            else:
                self.console.warn("已跳过备份（--skip-backup）；失败后无法自动回滚")
            self._rolling_upgrade(exact)
            self.current_version = exact
            self.record.add_history("upgrade", "%s -> %s" % (
                self.record.version, exact))
            self.save_state()
            self.refresh_kubeconfig()
            self.console.success("升级完成：%s" % exact)
        finally:
            for rt in self.runtimes.values():
                rt.close()

    def _preflight(self, rts, mode):
        errors = []
        for rt in rts:
            data = rt.call("preflight", {
                "version": self.spec.version, "role": rt.node.role,
                "mode": mode, "node_ip": rt.node_ip, "node_name": rt.hostname,
                "cni": self.spec.cni,
                "calico_encapsulation": self.spec.calico_encapsulation,
                "vip_port": self.spec.vip_port,
                "ha": self.spec.use_internal_ha and rt.node.role == ROLE_MASTER,
                "single_node": self.spec.node_count == 1,
                "allow_untested_os": getattr(self.options, "allow_untested_os", False),
                "keep_swap": not self.spec.disable_swap,
            }, timeout=180, stream=False)
            for warning in data.get("warnings", []):
                self.console.warn("%s：%s" % (rt.node.address, warning))
            for error in data.get("errors", []):
                self.console.error("%s：%s" % (rt.node.address, error))
                errors.append("%s: %s" % (rt.node.address, error))
        if errors:
            raise PreflightError("预检未通过，已停止", hint="；".join(errors))

    def _rolling_upgrade(self, exact):
        first = self.first_rt()
        all_rt = self.all_runtimes()
        order = self.master_runtimes() + [self.runtimes[n.address] for n in self.spec.workers]
        self.console.section("滚动升级节点")
        for idx, rt in enumerate(order):
            self.console.step("升级 %s（%d/%d）" % (rt.node.address, idx + 1, order.__len__()))
            drain = first.call("drain_node", {"node_name": rt.hostname}, timeout=420, stream=True)
            if not drain.get("ok"):
                raise KubeDeployError("节点 %s drain 失败，停止升级" % rt.node.address,
                                      hint=drain.get("output", ""))
            try:
                if rt.node.role == ROLE_MASTER and rt.node.address == self.spec.first_master.address:
                    rt.call("upgrade_apply", {
                        "target": exact, "repo_base": self.spec.package_repo_base,
                        "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                        "image_repositories": _image_repos(self.spec),
                    }, timeout=3600, stream=True)
                elif rt.node.role == ROLE_MASTER:
                    rt.call("upgrade_control_plane_node", {
                        "target": exact, "repo_base": self.spec.package_repo_base,
                        "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                    }, timeout=2400, stream=True)
                else:
                    rt.call("upgrade_worker_node", {
                        "target": exact, "repo_base": self.spec.package_repo_base,
                        "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                    }, timeout=2400, stream=True)
            finally:
                first.call("uncordon_node", {"node_name": rt.hostname}, timeout=120, stream=False)
            first.call("wait_ready", {"node_names": [rt.hostname], "timeout": 300},
                       timeout=360, stream=True)
            self.console.success("%s：升级并 Ready" % rt.node.address)
        info = first.call("cluster_info", {}, timeout=180, stream=False)
        versions = {n.get("version", "") for n in info.get("nodes", [])}
        if versions and versions != {exact}:
            raise KubeDeployError("节点版本未全部达到 %s：%s" % (exact, ", ".join(sorted(versions))))
        self.console.success("全部节点已滚动升级到 %s" % exact)

    # ---------- 降级/回滚 ----------
    def downgrade(self, target, confirm=False):
        target = self._clean_version(target)
        if not target:
            raise ValidationError("降级目标版本无效")
        if compare_versions(target, self.current_version) >= 0:
            raise ValidationError("降级目标 %s 必须低于当前版本 %s" % (target, self.current_version))
        candidates = [b for b in (self.record.backups or [])
                      if b.get("from_version") == target and b.get("nodes")]
        if not candidates:
            raise KubeDeployError(
                "找不到从 %s 升级前创建的备份，无法安全回滚" % target,
                hint="Kubernetes 不支持原地降级；本工具只支持恢复到升级前 etcd 快照。"
                     "如无备份，请重建或从企业备份系统恢复。")
        backup = sorted(candidates, key=lambda b: b.get("created_at", ""))[-1]
        master_addrs = {n.address for n in self.spec.masters}
        backup_nodes = {n.get("address"): n for n in backup.get("nodes", [])}
        if master_addrs != set(backup_nodes):
            raise KubeDeployError("当前控制平面拓扑与备份时不一致，拒绝回滚",
                                  hint="请恢复为备份时的 master 集合，或手工恢复 etcd")
        self.build_runtimes()
        try:
            self.spec.version = target
            self._preflight(self.all_runtimes(), "downgrade")
            for rt in self.all_runtimes():
                probe_node = rt.call("check_packages", {
                    "version_req": _version_req_dict(target),
                    "exact_version": target,
                    "repo_base": self.spec.package_repo_base,
                    "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                    "components": ["kubelet", "kubeadm", "kubectl"],
                }, timeout=600, stream=True)
                if probe_node.get("k8s_version") != target:
                    raise KubeDeployError("节点 %s 无法解析回滚目标版本 %s" % (
                        rt.node.address, target))
            self.console.warn("回滚将恢复 %s 的 etcd 快照；升级后写入的数据会丢失！" % backup["created_at"])
            # 读取第一个控制平面快照，提前验证
            first_master = self.first_rt()
            first_meta = backup_nodes[first_master.node.address]
            snap_resp = first_master.call("read_backup_file", {
                "snapshot_path": first_meta["snapshot_path"],
                "max_size": 512 * 1024 * 1024,
            }, timeout=1800, stream=True)
            snapshot_b64 = snap_resp["snapshot_b64"]
            snapshot_sha = snap_resp["sha256"]
            # 在停机前验证每个控制平面的本地归档，避免恢复阶段才发现文件缺失
            for node in self.spec.masters:
                meta = backup_nodes[node.address]
                self.runtimes[node.address].call("verify_backup", {
                    "backup_id": backup["backup_id"],
                    "node_name": meta.get("node_name") or node.hostname,
                    # 其他控制平面会用第一个 master 的快照覆盖本地快照
                    "skip_snapshot": node.address != self.spec.first_master.address,
                }, timeout=300, stream=True)
            # 停止所有 kubelet
            self.console.section("停止 kubelet 并准备回滚")
            for rt in self.all_runtimes():
                rt.call("kubelet_action", {"operation": "stop"}, timeout=180, stream=True)
            # 安装目标版本包
            try:
                for rt in self.all_runtimes():
                    rt.call("install_packages", {
                        "version_req": _version_req_dict(target),
                        "exact_version": target,
                        "repo_base": self.spec.package_repo_base,
                        "allow_upstream_fallback": self.spec.allow_upstream_fallback,
                        "components": ["kubelet", "kubeadm", "kubectl"],
                        "install_deps": False,
                        "include_addons": False,
                    }, timeout=2400, stream=True)
                    rt.call("kubelet_action", {"operation": "stop"}, timeout=180, stream=False)
            except Exception:
                for rt in self.runtimes.values():
                    try:
                        rt.call("kubelet_action", {"operation": "start"}, timeout=180, stream=False)
                    except Exception:
                        pass
                raise
            # 分发快照到其他控制平面节点
            for node in self.spec.masters[1:]:
                rt = self.runtimes[node.address]
                meta = backup_nodes[node.address]
                rt.call("write_backup_file", {
                    "backup_id": backup["backup_id"],
                    "node_name": meta.get("node_name") or rt.hostname,
                    "dest_path": meta.get("snapshot_path"),
                    "data_b64": snapshot_b64,
                    "sha256": snapshot_sha,
                }, timeout=1800, stream=True)
            # 恢复每个控制平面
            for node in self.spec.masters:
                rt = self.runtimes[node.address]
                meta = backup_nodes[node.address]
                rt.call("restore_control_plane", {
                    "backup_id": backup["backup_id"],
                    "node_name": meta.get("node_name") or rt.hostname,
                    "snapshot_path": (first_meta["snapshot_path"] if rt.node.address == self.spec.first_master.address
                                      else meta.get("snapshot_path")),
                    "snapshot_sha256": snapshot_sha,
                    "restore_archive": True,
                    "start": False,
                }, timeout=1800, stream=True)
                self.console.success("%s：etcd/控制平面已恢复" % rt.node.address)
            # 启动控制平面（同时启动以满足 etcd quorum）
            self.console.section("启动控制平面")
            for node in self.spec.masters:
                self.runtimes[node.address].call("start_node", {}, timeout=180, stream=False)
            self._wait_api_after_restore(first_master)
            # 启动 worker
            for node in self.spec.workers:
                self.runtimes[node.address].call("start_node", {}, timeout=180, stream=False)
            names = [self.runtimes[n.address].hostname for n in self.spec.all_nodes]
            first_master.call("wait_ready", {"node_names": names, "timeout": 600},
                              timeout=660, stream=True)
            info = first_master.call("cluster_info", {}, timeout=180, stream=False)
            versions = {n.get("version", "") for n in info.get("nodes", []) if n.get("version")}
            if versions and versions != {target}:
                raise KubeDeployError("回滚后节点版本不一致：期望 %s，实际 %s" % (
                    target, ", ".join(sorted(versions))),
                    hint="检查 kubelet 包是否在所有节点完成降级")
            self.current_version = target
            self.record.add_history("downgrade", "从 %s 回滚到 %s（备份 %s）" % (
                self.record.version, target, backup["backup_id"]))
            self.save_state()
            self.refresh_kubeconfig()
            self.console.success("回滚完成：%s（备份时间点之后的数据已丢失）" % target)
        finally:
            for rt in self.runtimes.values():
                rt.close()

    def _wait_api_after_restore(self, first_rt, timeout=600):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                info = first_rt.call("cluster_info", {}, timeout=60, stream=False)
                if info.get("version"):
                    return info
            except Exception as exc:
                last = str(exc)
            time.sleep(5)
        raise KubeDeployError("回滚后 API Server 未恢复", hint=last[-1000:] or
                              "检查 etcd 容器日志和证书是否与备份一致")
