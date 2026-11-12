"""内置控制平面高可用：HAProxy + Keepalived（VIP）。"""

from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
import socket
import string
import time

from .agent_base import (ActionFailure, backup_file, ensure_dir, log, read_file,
                         run, run_ok, service_active, write_file)
from .agent_platform import detect
from .util import is_ipv4


HAPROXY_CONF = "/etc/haproxy/haproxy.cfg"
KEEPALIVED_CONF = "/etc/keepalived/keepalived.conf"


def _install_packages():
    facts = detect()
    pm = facts.get("pkg_mgr")
    if pm == "apt":
        run_ok(["apt-get", "update"], timeout=600)
        proc = run(["apt-get", "install", "-y", "--no-install-recommends", "haproxy", "keepalived"],
                   timeout=600, check=False)
    elif pm in ("dnf", "yum"):
        exe = shutil.which("dnf") or shutil.which("yum")
        proc = run([exe, "install", "-y", "haproxy", "keepalived"], timeout=600, check=False)
        if proc.returncode != 0 and os.path.exists("/etc/yum.repos.d/kube-deploy-vault.repo"):
            proc = run([exe, "install", "-y", "--disablerepo=*",
                        "--enablerepo=kube-deploy-vault", "haproxy", "keepalived"],
                       timeout=900, check=False)
    else:
        raise ActionFailure("无法安装 HAProxy/Keepalived：不支持的包管理器")
    if proc.returncode != 0 or not shutil.which("haproxy") or not shutil.which("keepalived"):
        raise ActionFailure("HAProxy/Keepalived 安装失败",
                            errors=[proc.stderr.strip()[-1000:]],
                            hint="请配置可用的 OS 软件源，或部署外部负载均衡器")


def _vip_interface(params, facts):
    iface = params.get("vip_interface")
    if iface:
        return iface
    # 选择与 VIP 同网段的接口
    vip = params.get("vip")
    try:
        vip_obj = __import__("ipaddress").ip_address(vip)
    except Exception:
        vip_obj = None
    for item in facts.get("ips", []):
        if vip_obj is None:
            continue
        try:
            net = __import__("ipaddress").ip_network(item["cidr"], strict=False)
            if vip_obj in net:
                return item["interface"]
        except Exception:
            continue
    return facts.get("default_iface") or "eth0"


def _prefix_for_interface(facts, iface):
    for item in facts.get("ips", []):
        if item["interface"] == iface:
            return int(item["prefix"])
    return 24


def _vrrp_id(cluster_name, masters=None):
    # 只依赖集群名，保证扩容 master 后 VRID 不变，避免 VRRP 双主。
    text = cluster_name or "kubedeploy"
    return (int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 200) + 1


def _vrrp_password(cluster_name):
    seed = (cluster_name or "k8s") + "|kubedeploy-vrrp"
    alphabet = string.ascii_letters + string.digits
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(alphabet[b % len(alphabet)] for b in h[:8])


def _haproxy_config(params):
    port = int(params.get("port") or 16443)
    masters = params.get("masters") or []
    lines = [
        "global",
        "    maxconn 10000",
        "",
        "defaults",
        "    mode tcp",
        "    option tcplog",
        "    timeout connect 5s",
        "    timeout client 1h",
        "    timeout server 1h",
        "",
        "frontend kube-apiserver",
        "    bind *:%d" % port,
        "    default_backend kube-apiserver",
        "",
        "backend kube-apiserver",
        "    balance roundrobin",
    ]
    for idx, master in enumerate(masters, 1):
        lines.append("    server master-%d %s:6443 check inter 2s fall 3 rise 2" % (idx, master))
    return "\n".join(lines) + "\n"


def _keepalived_config(params, facts):
    vip = params.get("vip")
    iface = params.get("vip_interface") or _vip_interface(params, facts)
    masters = params.get("masters") or []
    current = params.get("node_ip")
    others = [x for x in masters if x != current]
    prefix = _prefix_for_interface(facts, iface)
    priority = int(params.get("priority") or (150 if params.get("is_first") else 100))
    vrid = int(params.get("vrrp_id") or _vrrp_id(params.get("cluster_name"), masters))
    password = params.get("vrrp_password") or _vrrp_password(params.get("cluster_name"))
    lines = [
        "global_defs {",
        "    router_id KUBEDEPLOY_%s" % re.sub(r"[^A-Za-z0-9_]", "_", params.get("cluster_name") or "K8S"),
        "}",
        "",
        "vrrp_instance KUBE_API {",
        "    state BACKUP",
        "    interface %s" % iface,
        "    virtual_router_id %d" % vrid,
        "    priority %d" % priority,
        "    advert_int 1",
        "    unicast_src_ip %s" % current,
    ]
    if others:
        lines.append("    unicast_peer {")
        for ip in others:
            lines.append("        %s" % ip)
        lines.append("    }")
    lines += [
        "    authentication {",
        "        auth_type PASS",
        "        auth_pass %s" % password,
        "    }",
        "    virtual_ipaddress {",
        "        %s/%d dev %s" % (vip, prefix, iface),
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def setup_ha(params):
    if not params.get("vip") or not params.get("masters"):
        raise ActionFailure("HA 配置缺少 VIP 或 master 列表")
    if not is_ipv4(params["vip"]):
        raise ActionFailure("当前 HA 方案仅支持 IPv4 VIP")
    facts = detect()
    iface = params.get("vip_interface") or _vip_interface(params, facts)
    params["vip_interface"] = iface
    if run_ok(["ip", "link", "show", "dev", iface], timeout=20).returncode != 0:
        raise ActionFailure("VIP 网卡不存在：%s" % iface,
                            hint="请通过 --vip-interface 指定正确网卡")
    _install_packages()
    backup_file(HAPROXY_CONF)
    backup_file(KEEPALIVED_CONF)
    ensure_dir(os.path.dirname(HAPROXY_CONF))
    ensure_dir(os.path.dirname(KEEPALIVED_CONF))
    ensure_dir("/run/haproxy", 0o755)

    haproxy_text = _haproxy_config(params)
    write_file(HAPROXY_CONF, haproxy_text, 0o644)
    check = run_ok(["haproxy", "-c", "-f", HAPROXY_CONF], timeout=60)
    if check.returncode != 0:
        raise ActionFailure("HAProxy 配置校验失败", errors=[check.stderr[-1000:]])
    keepalived_text = _keepalived_config(params, facts)
    write_file(KEEPALIVED_CONF, keepalived_text, 0o600)
    # firewalld/ufw 放行 HA 端口
    if service_active("firewalld"):
        run_ok(["firewall-cmd", "--permanent", "--add-port=%d/tcp" % int(params.get("port") or 16443)], timeout=30)
        run_ok(["firewall-cmd", "--permanent", "--add-protocol=vrrp"], timeout=30)
        run(["firewall-cmd", "--reload"], timeout=60, check=False)
    elif shutil.which("ufw"):
        proc = run_ok(["ufw", "status"], timeout=10)
        if "Status: active" in (proc.stdout or ""):
            run_ok(["ufw", "allow", "%d/tcp" % int(params.get("port") or 16443)], timeout=30)
            run_ok(["ufw", "allow", "proto", "vrrp"], timeout=30)
    run(["systemctl", "daemon-reload"], timeout=60, check=False)
    run(["systemctl", "enable", "keepalived"], timeout=60, check=False)
    run(["systemctl", "restart", "keepalived"], timeout=120, check=False)
    if not service_active("keepalived"):
        raise ActionFailure("keepalived 启动失败",
                            hint="执行 journalctl -u keepalived -n 100 查看 VRRP/网络错误")
    run(["systemctl", "enable", "haproxy"], timeout=60, check=False)
    run(["systemctl", "restart", "haproxy"], timeout=120, check=False)
    if not service_active("haproxy"):
        raise ActionFailure("HAProxy 启动失败",
                            hint="执行 journalctl -u haproxy -n 100 查看端口占用/配置错误")
    time.sleep(3)
    return {
        "vip": params["vip"],
        "interface": iface,
        "port": int(params.get("port") or 16443),
        "vrrp_id": int(params.get("vrrp_id") or _vrrp_id(params.get("cluster_name"), params["masters"])),
        "haproxy": HAPROXY_CONF,
        "keepalived": KEEPALIVED_CONF,
    }


def ha_status(params):
    proc = run_ok(["ip", "-o", "-4", "addr", "show"], timeout=20)
    vip = params.get("vip") or ""
    has_vip = bool(vip and re.search(r"\sinet\s+%s/" % re.escape(vip), proc.stdout or ""))
    haproxy = service_active("haproxy")
    keepalived = service_active("keepalived")
    return {"node": params.get("node_ip") or socket.gethostname(), "vip": vip,
            "has_vip": has_vip, "haproxy": haproxy, "keepalived": keepalived}


def teardown_ha(params):
    for unit in ("haproxy", "keepalived"):
        run_ok(["systemctl", "disable", "--now", unit], timeout=60)
    for path in (HAPROXY_CONF, KEEPALIVED_CONF):
        backup_file(path)
    return {"teardown": True}
