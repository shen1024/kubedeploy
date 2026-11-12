"""KubeDeploy 命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from types import SimpleNamespace

from . import __version__
from .agent_client import NodeAgentClient
from .constants import (DEFAULT_DNS_DOMAIN, DEFAULT_FLANNEL_CNI_IMAGE,
                        DEFAULT_FLANNEL_IMAGE, DEFAULT_IMAGE_REPOSITORY,
                        DEFAULT_POD_CIDR, DEFAULT_SERVICE_CIDR, DEFAULT_VIP_PORT,
                        K8S_MAX_MINOR, K8S_MIN_MINOR, APP_NAME, VERSION)
from .errors import (AgentError, KubeDeployError, PreflightError, StateError,
                     TransportError, UserAbort, ValidationError)
from .inventory import describe_inventory
from .lifecycle import ExistingCluster
from .models import (ClusterSpec, NodeSpec, ROLE_MASTER, ROLE_WORKER,
                     SshOptions, build_spec)
from .orchestrator import Deployer
from .state import ClusterRecord, cluster_dir, kubeconfig_dir, state_home
from .ui import Console
from .util import (is_local_address, normalize_address, parse_version_tuple,
                   safe_slug, split_tokens)
from .versions import parse_version_request, validate_upgrade_path


def _add_common(parser):
    g = parser.add_argument_group("通用选项")
    g.add_argument("--yes", action="store_true", help="自动确认所有非数据丢失类提示")
    g.add_argument("--dry-run", action="store_true", help="只打印计划，不连接/修改节点")
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--no-color", action="store_true")
    g.add_argument("--log-file", default=None)
    g.add_argument("--ssh-user", default=os.environ.get("KUBEDEPLOY_SSH_USER", "root"))
    g.add_argument("--ssh-port", type=int, default=int(os.environ.get("KUBEDEPLOY_SSH_PORT", "22")))
    g.add_argument("--ssh-key", default=os.environ.get("KUBEDEPLOY_SSH_KEY"))
    g.add_argument("--ssh-password", default=os.environ.get("KUBEDEPLOY_SSH_PASSWORD"),
                   help="不推荐；可用 KUBEDEPLOY_SSH_PASSWORD 环境变量")
    g.add_argument("--ssh-proxy-jump", default=None)
    g.add_argument("--ssh-strict-host-key-checking", default="accept-new",
                   choices=["yes", "no", "accept-new"])
    g.add_argument("--ssh-known-hosts", default=None)
    g.add_argument("--ssh-option", action="append", default=[])
    g.add_argument("--ssh-connect-timeout", type=int, default=20)
    g.add_argument("--no-become", action="store_true", help="禁用远程 sudo")
    g.add_argument("--force-remote", action="store_true", help="即使 IP 属于本机也走 SSH")
    g.add_argument("--force-upload", action="store_true", help="强制重新上传 agent")
    g.add_argument("--no-auto-node-ip", action="store_true",
                   help="SSH 地址不是节点本地 IP 时，不自动选择节点默认网卡 IP")
    g.add_argument("--allow-untested-os", action="store_true")
    g.add_argument("--containerd-version", default=None)
    g.add_argument("--runc-version", default=None)
    g.add_argument("--containerd-sha256", default=None,
                   help="自定义 containerd 版本时提供可信 SHA256")
    g.add_argument("--runc-sha256", default=None,
                   help="自定义 runc 版本时提供可信 SHA256")
    g.add_argument("--skip-backup", action="store_true", help="升级前跳过 etcd 备份（危险，禁止回滚）")


def _add_spec_options(parser):
    parser.add_argument("--version", dest="version", default="1.37",
                        help="Kubernetes 版本：1.28-1.37 / v1.37.2")
    parser.add_argument("--name", default=None, help="集群名称")
    parser.add_argument("--pod-cidr", default=DEFAULT_POD_CIDR)
    parser.add_argument("--service-cidr", default=DEFAULT_SERVICE_CIDR)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--dns-domain", default=DEFAULT_DNS_DOMAIN)
    parser.add_argument("--cni", default="calico", choices=["calico", "flannel"],
                        help="CNI：calico（默认）或 flannel")
    parser.add_argument("--calico-encapsulation", default="ipip",
                        choices=["ipip", "vxlan", "none"],
                        help="Calico 封装：ipip（默认）/vxlan/none(BGP)")
    parser.add_argument("--calico-image-repository",
                        default="docker.m.daocloud.io/calico",
                        help="Calico 镜像前缀（默认 DaoCloud 国内代理）")
    parser.add_argument("--image-repository", default=DEFAULT_IMAGE_REPOSITORY)
    parser.add_argument("--flannel-image", default=DEFAULT_FLANNEL_IMAGE)
    parser.add_argument("--flannel-cni-image", default=DEFAULT_FLANNEL_CNI_IMAGE)
    parser.add_argument("--pod-iface", default=None)
    parser.add_argument("--package-repo-base", default=None,
                        help="企业内网 K8s 仓库根，例如 https://mirror/k8s/core/stable")
    parser.add_argument("--no-upstream-fallback", action="store_true",
                        help="国内源缺失目标版本时失败，不自动回退官方源")
    parser.add_argument("--keep-swap", action="store_true")
    parser.add_argument("--selinux", default="permissive", choices=["permissive", "enforcing"])
    parser.add_argument("--no-firewall", action="store_true")
    parser.add_argument("--node-port-range", default="30000-32767")
    parser.add_argument("--node-ip", default=None,
                        help="单节点时显式指定节点私网 IP（公有云 NAT 场景）")
    parser.add_argument("--allow-control-plane-without-ha", action="store_true",
                        help="允许没有 VIP/外部 LB 的多 master（不推荐）")
    parser.add_argument("--vip", default=None)
    parser.add_argument("--vip-interface", default=None)
    parser.add_argument("--vip-port", type=int, default=DEFAULT_VIP_PORT)
    parser.add_argument("--control-plane-endpoint", default=None,
                        help="外部负载均衡端点 host:port")
    parser.add_argument("--external-endpoint", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kubedeploy",
        description="KubeDeploy Enterprise - 企业级 Kubernetes 一键部署/扩容/升级/回滚",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用示例：
  kubedeploy single --node 10.0.0.11 --version 1.34.6
  kubedeploy single --node 47.100.1.10 --node-ip 172.16.0.10 --version 1.34.6
  kubedeploy cluster --masters 47.100.1.11=172.16.0.11,47.100.1.12=172.16.0.12,47.100.1.13=172.16.0.13 --workers 47.100.1.21=172.16.0.21 --control-plane-endpoint k8s-lb.example.com:6443 --external-endpoint --version 1.34.6
  kubedeploy cluster --masters 10.0.0.11,10.0.0.12,10.0.0.13 --workers 10.0.0.21,10.0.0.22 --vip 10.0.0.100 --version 1.34.6
  kubedeploy expand --bootstrap 10.0.0.11 --masters 10.0.0.14 --workers 10.0.0.23
  kubedeploy upgrade --bootstrap 10.0.0.11 --to 1.35.1
  kubedeploy downgrade --bootstrap 10.0.0.11 --to 1.34.6
  kubedeploy status --bootstrap 10.0.0.11
""")
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)

    p = sub.add_parser("single", parents=[common], help="部署单节点集群")
    _add_spec_options(p)
    p.add_argument("--node", required=True,
                   help="单节点 SSH 地址；公有云可写 公网IP=私网IP")

    p = sub.add_parser("cluster", parents=[common], help="部署多节点集群")
    _add_spec_options(p)
    p.add_argument("--masters", required=True,
                   help="master SSH 地址，逗号分隔；可写 公网IP=私网IP")
    p.add_argument("--workers", default="",
                   help="worker SSH 地址，逗号分隔；可写 公网IP=私网IP")

    p = sub.add_parser("expand", parents=[common], help="已有集群扩容")
    p.add_argument("--bootstrap", required=True, help="现有控制平面 IP")
    p.add_argument("--masters", default="",
                   help="新增 master SSH 地址，逗号分隔；可写 公网IP=私网IP")
    p.add_argument("--workers", default="",
                   help="新增 worker SSH 地址，逗号分隔；可写 公网IP=私网IP")
    p.add_argument("--vip-interface", default=None)
    p.add_argument("--allow-control-plane-without-ha", action="store_true")

    p = sub.add_parser("upgrade", parents=[common], help="滚动升级 Kubernetes")
    p.add_argument("--bootstrap", required=True)
    p.add_argument("--to", default=None, help="目标版本 1.28-1.37")

    p = sub.add_parser("downgrade", parents=[common], help="从升级前备份回滚")
    p.add_argument("--bootstrap", required=True)
    p.add_argument("--to", default=None)
    p.add_argument("--confirm-data-loss", action="store_true",
                   help="确认恢复快照会丢失升级后数据")

    p = sub.add_parser("status", parents=[common], help="查看现有集群状态")
    p.add_argument("--bootstrap", required=True)

    p = sub.add_parser("reset", parents=[common], help="清理单个节点的 Kubernetes 状态（危险）")
    p.add_argument("--node", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("menu", parents=[common], help="交互式菜单")
    return parser


def _console(args):
    log_dir = os.path.join(state_home(), "logs")
    log_file = getattr(args, "log_file", None)
    if not log_file:
        log_file = os.path.join(log_dir, "kubedeploy-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    con = Console(verbose=getattr(args, "verbose", False),
                  color=False if getattr(args, "no_color", False) else None,
                  log_file=log_file)
    return con


def _ssh_options(args):
    return SshOptions(
        user=args.ssh_user, port=args.ssh_port, key=args.ssh_key,
        password=args.ssh_password, proxy_jump=args.ssh_proxy_jump,
        strict_host_key_checking=args.ssh_strict_host_key_checking,
        known_hosts=args.ssh_known_hosts, extra_options=args.ssh_option,
        connect_timeout=args.ssh_connect_timeout, become=not args.no_become,
    )


def _spec_from_install_args(args, masters, workers):
    ns = SimpleNamespace(**vars(args))
    ns.masters = masters
    ns.workers = workers
    ns.external_endpoint = bool(getattr(args, "external_endpoint", False) or
                                getattr(args, "control_plane_endpoint", None))
    ns.allow_control_plane_without_ha = getattr(args, "allow_control_plane_without_ha", False)
    ns.no_upstream_fallback = getattr(args, "no_upstream_fallback", False)
    return build_spec(ns)


def _run_install(args, console, single=False):
    if single:
        masters = [args.node]
        workers = []
    else:
        masters = split_tokens(args.masters)
        workers = split_tokens(args.workers)
    spec = _spec_from_install_args(args, masters, workers)
    spec.validate()
    console.raw(describe_inventory(spec))
    if not args.yes and not args.dry_run and not args.preflight_only:
        if not console.confirm("确认按上述清单部署？", default=False):
            raise UserAbort("用户取消")
    options = SimpleNamespace(
        dry_run=args.dry_run, preflight_only=args.preflight_only,
        force_remote=args.force_remote, force_upload=args.force_upload,
        allow_untested_os=args.allow_untested_os,
        containerd_version=args.containerd_version,
        runc_version=args.runc_version,
        containerd_sha256=args.containerd_sha256,
        runc_sha256=args.runc_sha256, yes=args.yes,
        auto_node_ip=not args.no_auto_node_ip,
    )
    deployer = Deployer(spec, console, options)
    if args.preflight_only:
        deployer.preflight()
    else:
        deployer.install()
    return 0


def _workflow_single(args, console):
    return _run_install(args, console, single=True)


def _workflow_cluster(args, console):
    return _run_install(args, console, single=False)


def _existing(args, console):
    return ExistingCluster(args.bootstrap, _ssh_options(args), console, args)


def _workflow_expand(args, console):
    if args.dry_run:
        console.info("[dry-run] 将连接 %s 并扩容" % args.bootstrap)
        return 0
    existing = _existing(args, console)
    try:
        console.raw(describe_inventory(existing.spec))
        new_masters = split_tokens(args.masters)
        new_workers = split_tokens(args.workers)
        if not new_masters and not new_workers:
            raise ValidationError("必须指定 --masters 或 --workers")
        total = existing.spec.node_count + len(new_masters) + len(new_workers)
        console.info("本次新增 %d 台，扩容后总节点数：%d" % (
            len(new_masters) + len(new_workers), total))
        if new_masters and not existing.spec.use_internal_ha and not existing.spec.external_endpoint:
            if not args.allow_control_plane_without_ha:
                raise ValidationError(
                    "原集群没有 VIP/外部负载均衡端点，新增 master 需要 --allow-control-plane-without-ha",
                    hint="更推荐先为控制平面配置稳定端点和 HA，再扩容 master")
            existing.spec.allow_control_plane_without_ha = True
        if not args.yes and not console.confirm("确认扩容？", default=False):
            raise UserAbort("用户取消")
        existing.expand(new_masters, new_workers)
        return 0
    finally:
        existing.close()


def _workflow_upgrade(args, console):
    if args.dry_run:
        console.info("[dry-run] 将连接 %s 并升级到 %s" % (args.bootstrap, args.to or "<交互输入>"))
        return 0
    existing = _existing(args, console)
    try:
        console.info("当前版本：%s    控制平面端点：%s" % (
            existing.current_version, existing.spec.endpoint))
        target = args.to or console.ask("升级目标版本", default="")
        if not target:
            raise ValidationError("必须指定目标版本")
        parse_version_request(target)
        if args.skip_backup:
            console.warn("已请求跳过 etcd 备份；升级失败时无法自动回滚。")
            if not args.yes and not console.confirm("确认跳过备份并继续？", default=False):
                raise UserAbort("用户取消")
        elif not args.yes:
            if not console.confirm("升级会自动备份 etcd；确认继续？", default=False):
                raise UserAbort("用户取消")
        existing.upgrade(target)
        return 0
    finally:
        existing.close()


def _workflow_downgrade(args, console):
    if args.dry_run:
        console.info("[dry-run] 将连接 %s 并回滚到 %s" % (args.bootstrap, args.to or "<交互选择备份>"))
        return 0
    existing = _existing(args, console)
    try:
        backups = existing.record.backups or []
        if not backups:
            raise KubeDeployError(
                "没有可用的升级前备份，无法降级",
                hint="Kubernetes 不支持原地降级；请通过备份系统恢复，或重建集群。")
        console.info("当前版本：%s" % existing.current_version)
        console.table([[b.get("from_version", ""), b.get("to_version", ""),
                        b.get("backup_id", ""), b.get("created_at", "")]
                       for b in backups],
                      headers=["可回滚到", "当时升级到", "备份 ID", "时间"])
        target = args.to
        if not target:
            target = console.ask("回滚目标版本", default=backups[-1].get("from_version", ""))
        if not target:
            raise ValidationError("必须指定回滚目标版本")
        target = existing._clean_version(target)
        candidates = [b for b in backups if b.get("from_version") == target]
        if not candidates:
            raise KubeDeployError("没有版本 %s 对应的升级前备份" % target,
                                  hint="只能回滚到曾由本工具升级前备份的版本")
        console.warn("回滚会把 etcd 恢复到备份时间点，备份之后写入的数据将永久丢失！")
        if not args.confirm_data_loss:
            if args.yes:
                raise ValidationError("--yes 不能替代数据丢失确认，请增加 --confirm-data-loss")
            if not console.require_phrase("此操作不可逆。", "ROLLBACK"):
                raise UserAbort("用户取消")
        existing.downgrade(target, confirm=True)
        return 0
    finally:
        existing.close()


def _workflow_status(args, console):
    if args.dry_run:
        console.info("[dry-run] 将连接 %s 查询状态" % args.bootstrap)
        return 0
    existing = _existing(args, console)
    try:
        console.raw(describe_inventory(existing.spec))
        info = existing.first_rt().call("cluster_info", {}, timeout=180, stream=True)
        rows = [[n.get("name", ""), n.get("role", ""), n.get("ip", ""),
                 n.get("version", ""), "Ready" if n.get("ready") else "NotReady"]
                for n in info.get("nodes", [])]
        console.table(rows, headers=["节点", "角色", "IP", "版本", "状态"])
        try:
            existing.refresh_kubeconfig()
        except Exception as exc:
            console.warn("刷新本地 kubeconfig 失败：%s" % exc)
        console.info("kubeconfig：%s" % existing.record.kubeconfig_path())
        return 0
    finally:
        existing.close()


def _workflow_reset(args, console):
    if args.dry_run:
        console.info("[dry-run] 将清理节点 %s 的 Kubernetes 状态" % args.node)
        return 0
    console.warn("将执行 kubeadm reset 并删除 /etc/kubernetes、/var/lib/kubelet、/var/lib/etcd、"
                 "/etc/cni/net.d、/var/lib/kube-deploy。此操作不可逆！")
    if not args.force and not console.require_phrase("确认清理该节点？", "RESET"):
        raise UserAbort("用户取消")
    client = NodeAgentClient(args.node, _ssh_options(args), console,
                             local=(is_local_address(args.node) and not args.force_remote))
    try:
        client.prepare()
        client.call("reset", {"force": True}, timeout=900, stream=True)
        console.success("节点 %s 已清理；containerd 软件包保留" % args.node)
        return 0
    finally:
        client.close()


def _interactive_cni(args, console):
    console.section("CNI 网络")
    args.cni = console.ask("CNI 类型 [calico/flannel]", default="calico").lower()
    if args.cni not in ("calico", "flannel"):
        raise ValidationError("CNI 只能是 calico 或 flannel")
    if args.cni == "calico":
        args.calico_encapsulation = console.ask(
            "Calico 封装 [ipip/vxlan/none]", default="ipip").lower()
        if args.calico_encapsulation not in ("ipip", "vxlan", "none"):
            raise ValidationError("Calico 封装只能是 ipip、vxlan 或 none")
        args.calico_image_repository = console.ask(
            "Calico 镜像前缀", default="docker.m.daocloud.io/calico")


def _interactive_ssh(args, console, address):
    if is_local_address(address) and not args.force_remote:
        return
    console.section("SSH 连接参数")
    if args.ssh_user == "root":
        args.ssh_user = console.ask("SSH 用户", default="root")
    if args.ssh_port == 22:
        try:
            args.ssh_port = int(console.ask("SSH 端口", default="22"))
        except ValueError:
            raise ValidationError("SSH 端口必须是数字")
    if not args.ssh_key:
        key = console.ask("SSH 私钥路径（留空使用 ssh-agent/默认密钥）", default="")
        if key:
            args.ssh_key = os.path.expanduser(key)
    if not args.ssh_password:
        pwd = console.ask("SSH 密码（留空不使用密码）", default="", secret=True)
        if pwd:
            args.ssh_password = pwd


def _menu_options():
    parser = build_parser()
    # 只取一份默认 Namespace，不经过 parse_args（避免 required）
    base = SimpleNamespace(
        yes=False, dry_run=False, verbose=False, no_color=False, log_file=None,
        ssh_user="root", ssh_port=22, ssh_key=None, ssh_password=None,
        ssh_proxy_jump=None, ssh_strict_host_key_checking="accept-new",
        ssh_known_hosts=None, ssh_option=[], ssh_connect_timeout=20,
        no_become=False, force_remote=False, force_upload=False,
        no_auto_node_ip=False, node_ip=None,
        allow_untested_os=False, containerd_version=None, runc_version=None,
        containerd_sha256=None, runc_sha256=None,
        skip_backup=False, confirm_data_loss=False,
        version="1.37", name=None, pod_cidr=DEFAULT_POD_CIDR,
        service_cidr=DEFAULT_SERVICE_CIDR, dns_domain=DEFAULT_DNS_DOMAIN,
        timezone="Asia/Shanghai",
        cni="calico", calico_encapsulation="ipip",
        calico_image_repository="docker.m.daocloud.io/calico",
        image_repository=DEFAULT_IMAGE_REPOSITORY,
        flannel_image=DEFAULT_FLANNEL_IMAGE,
        flannel_cni_image=DEFAULT_FLANNEL_CNI_IMAGE,
        pod_iface=None, package_repo_base=None, no_upstream_fallback=False,
        keep_swap=False, selinux="permissive", no_firewall=False,
        node_port_range="30000-32767", allow_control_plane_without_ha=False,
        vip=None, vip_interface=None, vip_port=DEFAULT_VIP_PORT,
        control_plane_endpoint=None, external_endpoint=False,
        preflight_only=False, to=None, masters="", workers="", node=None,
        bootstrap=None,
    )
    return base


def _menu(args, console):
    console.banner()
    console.raw("请选择功能：")
    console.raw("  1) 部署单节点集群")
    console.raw("  2) 部署多节点集群")
    console.raw("  3) 已有单节点集群扩容为多节点集群")
    console.raw("  4) 更新 Kubernetes 版本")
    console.raw("  5) 降级/回滚")
    console.raw("  6) 查看集群状态")
    console.raw("  0) 退出")
    choice = console.ask("功能编号", default="0")
    if choice == "0":
        return 0
    a = _menu_options()
    a.verbose = getattr(args, "verbose", False)
    a.no_color = getattr(args, "no_color", False)
    a.log_file = getattr(args, "log_file", None)
    a.force_remote = getattr(args, "force_remote", False)
    if choice == "1":
        a.node = console.ask("部署节点 SSH 地址（可写 公网IP=私网IP）")
        if not a.node:
            raise ValidationError("节点 IP 不能为空")
        _interactive_ssh(a, console, a.node.split("=", 1)[0])
        local = is_local_address(a.node.split("=", 1)[0]) and not a.force_remote
        if local:
            console.info("检测到目标 IP 属于本机，将直接在本机部署")
        if "=" not in a.node:
            a.node_ip = console.ask(
                "节点私网 IP（公有云 NAT/多网卡时填写，留空自动探测）", default="")
        a.control_plane_endpoint = console.ask(
            "外部负载均衡 API 端点（可选，host:port；留空使用节点地址）", default="")
        if a.control_plane_endpoint:
            a.external_endpoint = True
        a.version = console.ask("Kubernetes 版本", default="1.37")
        _interactive_cni(a, console)
        a.name = console.ask("集群名称", default="k8s-%s" % safe_slug(a.node.replace(".", "-")[-18:]))
        a.yes = console.confirm("是否自动确认剩余步骤？", default=True)
        return _workflow_single(a, console)
    if choice == "2":
        a.masters = console.ask(
            "master SSH 地址（逗号分隔；可写 公网IP=私网IP）")
        a.workers = console.ask(
            "worker SSH 地址（逗号分隔，可留空；可写 公网IP=私网IP）", default="")
        masters = split_tokens(a.masters)
        workers = split_tokens(a.workers)
        if not masters:
            raise ValidationError("至少一个 master")
        console.info("节点总数：%d（master %d / worker %d）" % (
            len(masters) + len(workers), len(masters), len(workers)))
        a.version = console.ask("Kubernetes 版本", default="1.37")
        _interactive_cni(a, console)
        a.name = console.ask("集群名称", default="k8s-%s" % safe_slug(
            masters[0].split("=", 1)[0].replace(".", "-")[-18:]))
        if len(masters) > 1:
            console.raw("控制平面访问方式：")
            console.raw("  1) 内置 VIP（Keepalived；要求二层网络/VRRP 可用，物理机、VMware、私有云）")
            console.raw("  2) 外部负载均衡（公有云 NLB/CLB/SLB 推荐）")
            mode = console.ask("选择", default="1")
            if mode == "2":
                a.control_plane_endpoint = console.ask(
                    "外部负载均衡 API 端点（host:port）")
                if not a.control_plane_endpoint:
                    raise ValidationError("外部负载均衡模式必须提供 host:port")
                a.external_endpoint = True
            else:
                a.control_plane_endpoint = console.ask(
                    "外部负载均衡 API 端点（可选；如已有 LB 可填，留空使用内置 VIP）",
                    default="")
                if a.control_plane_endpoint:
                    a.external_endpoint = True
                else:
                    a.vip = console.ask("控制平面 VIP")
                    if not a.vip:
                        raise ValidationError(
                            "多 master 必须提供 VIP 或外部负载均衡端点")
                    a.vip_interface = console.ask("VIP 网卡（留空自动探测）", default="")
        else:
            a.control_plane_endpoint = console.ask(
                "外部负载均衡 API 端点（可选，host:port）", default="")
            if a.control_plane_endpoint:
                a.external_endpoint = True
        _interactive_ssh(a, console, masters[0].split("=", 1)[0])
        a.yes = console.confirm("是否自动确认剩余步骤？", default=True)
        return _workflow_cluster(a, console)
    if choice == "3":
        a.bootstrap = console.ask("现有控制平面 IP")
        if not a.bootstrap:
            raise ValidationError("必须输入现有控制平面 IP")
        _interactive_ssh(a, console, a.bootstrap)
        existing = _existing(a, console)
        try:
            console.raw(describe_inventory(existing.spec))
            a.masters = console.ask(
                "新增 master SSH 地址（逗号分隔，可留空；可写 公网IP=私网IP）", default="")
            a.workers = console.ask(
                "新增 worker SSH 地址（逗号分隔，可留空；可写 公网IP=私网IP）", default="")
            if not a.masters and not a.workers:
                raise ValidationError("没有新增节点")
            total = existing.spec.node_count + len(split_tokens(a.masters)) + len(split_tokens(a.workers))
            console.info("扩容后节点总数：%d" % total)
            if split_tokens(a.masters) and not existing.spec.use_internal_ha and not existing.spec.external_endpoint:
                if not console.confirm("现有集群无控制平面 VIP/外部 LB，新增 master 不提供 API 故障切换，继续？", default=False):
                    raise UserAbort("用户取消")
                a.allow_control_plane_without_ha = True
            existing.expand(split_tokens(a.masters), split_tokens(a.workers))
            return 0
        finally:
            existing.close()
    if choice == "4":
        a.bootstrap = console.ask("现有控制平面 IP")
        _interactive_ssh(a, console, a.bootstrap)
        existing = _existing(a, console)
        try:
            console.info("当前版本：%s" % existing.current_version)
            a.to = console.ask("升级目标版本", default="")
            if not a.to:
                raise ValidationError("必须输入目标版本")
            if not console.confirm("升级会创建 etcd/控制平面备份并滚动重启节点，继续？", default=False):
                raise UserAbort("用户取消")
            existing.upgrade(a.to)
            return 0
        finally:
            existing.close()
    if choice == "5":
        a.bootstrap = console.ask("现有控制平面 IP")
        _interactive_ssh(a, console, a.bootstrap)
        existing = _existing(a, console)
        try:
            backups = existing.record.backups or []
            if not backups:
                raise KubeDeployError("没有升级前备份，无法降级",
                                      hint="Kubernetes 不支持原地降级；请从备份系统恢复。")
            console.table([[b.get("from_version", ""), b.get("backup_id", ""), b.get("created_at", "")]
                           for b in backups], headers=["回滚到", "备份 ID", "时间"])
            a.to = console.ask("回滚目标版本", default=backups[-1].get("from_version", ""))
            console.warn("回滚会丢失备份时间点之后的数据！")
            if not console.require_phrase("确认回滚？", "ROLLBACK"):
                raise UserAbort("用户取消")
            existing.downgrade(a.to, confirm=True)
            return 0
        finally:
            existing.close()
    if choice == "6":
        a.bootstrap = console.ask("控制平面 IP")
        _interactive_ssh(a, console, a.bootstrap)
        return _workflow_status(a, console)
    raise ValidationError("未知功能编号")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    console = _console(args)
    try:
        if not args.command:
            return _menu(args, console)
        if args.command == "single":
            return _workflow_single(args, console)
        if args.command == "cluster":
            return _workflow_cluster(args, console)
        if args.command == "expand":
            return _workflow_expand(args, console)
        if args.command == "upgrade":
            return _workflow_upgrade(args, console)
        if args.command == "downgrade":
            return _workflow_downgrade(args, console)
        if args.command == "status":
            return _workflow_status(args, console)
        if args.command == "reset":
            return _workflow_reset(args, console)
        if args.command == "menu":
            return _menu(args, console)
        raise ValidationError("未知命令：%s" % args.command)
    except UserAbort as exc:
        console.warn(str(exc))
        return 130
    except (KubeDeployError, ValidationError, TransportError, AgentError, PreflightError) as exc:
        console.error(exc.message if hasattr(exc, "message") else str(exc))
        if getattr(exc, "hint", None):
            console.info("提示：%s" % exc.hint)
        if getattr(exc, "details", None):
            for detail in exc.details[:20]:
                console.error("  - %s" % detail)
        return 2
    except KeyboardInterrupt:
        console.warn("用户中断")
        return 130
    except Exception as exc:
        console.error("未处理错误：%s" % exc)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        return 3
    finally:
        console.close()


if __name__ == "__main__":
    raise SystemExit(main())
