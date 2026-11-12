"""kubeadm 初始化、加入、节点状态与重置。"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import time

from .agent_base import (ActionFailure, ensure_dir, ensure_root, log, read_file,
                         remove_file, run, run_ok, service_active, wait_for,
                         write_file)
from .agent_platform import detect
from .agent_runtime import set_sandbox_image
from .constants import (CONTAINERD_SOCK, DEFAULT_IMAGE_REPOSITORY,
                        FALLBACK_IMAGE_REPOSITORY, UPSTREAM_IMAGE_REPOSITORY)
from .util import b64e, parse_version_tuple


KUBECONFIG = "/etc/kubernetes/admin.conf"
KUBELET_DROPIN = "/etc/systemd/system/kubelet.service.d/90-kube-deploy-node-ip.conf"


def _kubeadm(args, timeout=1800, check=True):
    return run(["kubeadm"] + args, timeout=timeout, check=check)


def _kubectl(args, timeout=300, check=True):
    return run(["kubectl", "--kubeconfig", KUBECONFIG] + args, timeout=timeout, check=check)


def _endpoint_host(endpoint):
    endpoint = re.sub(r"^https?://", "", endpoint or "")
    if endpoint.startswith("["):
        return endpoint[1:].split("]")[0]
    return endpoint.rsplit(":", 1)[0] if ":" in endpoint else endpoint


def _setup_kubelet_node_ip(node_ip, node_name=""):
    extra = "--node-ip=%s" % node_ip
    if node_name:
        extra += " --hostname-override=%s" % node_name
    ensure_dir(os.path.dirname(KUBELET_DROPIN))
    write_file(KUBELET_DROPIN, """[Service]
Environment="KUBELET_EXTRA_ARGS=%s"
""" % extra)
    run(["systemctl", "daemon-reload"], timeout=60, check=False)
    # 兼容旧版 kubelet 服务：同时写入常见环境文件
    for path in ("/etc/default/kubelet", "/etc/sysconfig/kubelet"):
        if os.path.isdir(os.path.dirname(path)):
            text = read_file(path)
            line = 'KUBELET_EXTRA_ARGS="%s"' % extra
            if "KUBELET_EXTRA_ARGS" in text:
                new = re.sub(r'(?m)^KUBELET_EXTRA_ARGS=.*$', line, text)
                if new != text:
                    write_file(path, new)
            else:
                write_file(path, text.rstrip() + "\n" + line + "\n")


def _image_repositories(params):
    requested = params.get("image_repositories") or []
    if not requested:
        requested = [params.get("image_repository") or DEFAULT_IMAGE_REPOSITORY,
                     FALLBACK_IMAGE_REPOSITORY]
        if params.get("allow_upstream_fallback", True):
            requested.append(UPSTREAM_IMAGE_REPOSITORY)
    result = []
    for item in requested:
        item = (item or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _image_list(version, repo):
    proc = _kubeadm(["config", "images", "list", "--kubernetes-version", version,
                     "--image-repository", repo], timeout=120, check=False)
    if proc.returncode != 0:
        raise ActionFailure("无法列出 Kubernetes 镜像（%s）" % repo,
                            errors=[proc.stderr.strip()[-800:]])
    images = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    pause = ""
    for image in images:
        if re.search(r"/pause(:|@)", image):
            pause = image
            break
    if not pause:
        raise ActionFailure("无法从 kubeadm 镜像列表中找到 pause 镜像")
    return images, pause


def _pull_images(version, repo, timeout=2400):
    proc = _kubeadm(["config", "images", "pull", "--kubernetes-version", version,
                     "--image-repository", repo, "--cri-socket", CONTAINERD_SOCK],
                    timeout=timeout, check=False)
    return proc


def _prepare_images(params):
    version = params.get("version")
    if not version or not parse_version_tuple(version):
        raise ActionFailure("缺少合法的 Kubernetes 版本")
    last_error = None
    for repo in _image_repositories(params):
        try:
            images, pause = _image_list(version, repo)
            log("使用镜像仓库 %s，pause=%s" % (repo, pause))
            set_sandbox_image({"pause_image": pause})
            proc = _pull_images(version, repo)
            if proc.returncode == 0:
                return {"repository": repo, "pause_image": pause, "images": images}
            last_error = proc.stderr.strip()[-1500:]
            log("镜像仓库 %s 拉取失败，尝试下一个" % repo)
        except ActionFailure as exc:
            last_error = "；".join(exc.errors)
            log("镜像仓库 %s 不可用：%s" % (repo, last_error))
    raise ActionFailure("所有 Kubernetes 镜像仓库均拉取失败",
                        errors=[last_error or "未知错误"],
                        hint="可配置 --image-repository 指向企业 Harbor；"
                             "检查 containerd 与镜像仓库网络")


def _init_command(params, hostname, image_repository):
    """构造 kubeadm init 参数；不要使用 kubeadm init 不支持的 --timeout。"""
    version = params.get("version")
    node_ip = params.get("node_ip")
    endpoint = re.sub(r"^https?://", "", params.get("endpoint") or "")
    cmd = ["init",
           "--kubernetes-version", version,
           "--image-repository", image_repository,
           "--control-plane-endpoint", endpoint,
           "--apiserver-advertise-address", node_ip,
           "--apiserver-cert-extra-sans", _endpoint_host(endpoint),
           "--pod-network-cidr", params.get("pod_cidr"),
           "--service-cidr", params.get("service_cidr") or "10.96.0.0/16",
           "--cri-socket", CONTAINERD_SOCK,
           "--node-name", hostname,
           "--upload-certs",
           "--v", "2"]
    if params.get("dns_domain"):
        cmd += ["--service-dns-domain", params["dns_domain"]]
    return cmd


def init_cluster(params):
    ensure_root()
    version = params.get("version")
    node_ip = params.get("node_ip")
    endpoint = re.sub(r"^https?://", "", params.get("endpoint") or "")
    hostname = params.get("node_name") or socket.gethostname()
    if not version or not node_ip or not endpoint:
        raise ActionFailure("init 参数不完整（version/node_ip/endpoint）")
    _setup_kubelet_node_ip(node_ip, hostname)
    image = _prepare_images(params)
    cmd = _init_command(params, hostname, image["repository"])
    log("执行 kubeadm init（端点 %s）" % endpoint)
    proc = _kubeadm(cmd, timeout=2400, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubeadm init 失败",
                            errors=[proc.stderr.strip()[-1800:]],
                            hint="查看 journalctl -u kubelet 和 /var/log/pods；"
                                 "修复后先执行 kubeadm reset -f 再重试")
    if not os.path.exists(KUBECONFIG):
        raise ActionFailure("kubeadm init 完成但未生成 admin.conf")
    _wait_api(timeout=300)
    admin_conf = read_file(KUBECONFIG)
    try:
        join = _join_credentials(control_plane=True)
    except ActionFailure as exc:
        # 控制平面已经起来；join 凭据失败不应把整个 init 判为失败。
        log("生成 join 凭据失败（可稍后在控制平面重试）：%s" % exc)
        join = {}
    return {
        "node_name": hostname,
        "endpoint": endpoint,
        "image_repository": image["repository"],
        "pause_image": image["pause_image"],
        "admin_conf_b64": b64e(admin_conf),
        "join": join,
        "version": version,
    }


def _wait_api(timeout=300):
    def check():
        proc = _kubectl(["get", "--raw=/readyz"], timeout=20, check=False)
        return proc.returncode == 0 and "ok" in (proc.stdout or "").lower()
    wait_for("API Server 就绪", check, timeout=timeout, interval=3)


def set_cluster_image_repository(repo):
    """更新 kubeadm-config 中的 imageRepository，供后续 join/upgrade 使用。"""
    proc = _kubectl(["-n", "kube-system", "get", "configmap", "kubeadm-config",
                     "-o", "jsonpath={.data.ClusterConfiguration}"], timeout=60, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ActionFailure("无法读取 kubeadm-config ConfigMap")
    text = proc.stdout
    new = re.sub(r"(?m)^imageRepository:\s*\S+\s*$",
                 "imageRepository: %s" % repo, text)
    if new == text and "imageRepository:" not in text:
        new = text.rstrip() + "\nimageRepository: %s\n" % repo
    if new != text:
        import json as _json
        payload = _json.dumps({"data": {"ClusterConfiguration": new}})
        proc = _kubectl(["-n", "kube-system", "patch", "configmap", "kubeadm-config",
                         "--type", "merge", "-p", payload], timeout=60, check=False)
        if proc.returncode != 0:
            raise ActionFailure("更新 kubeadm-config imageRepository 失败",
                                errors=[proc.stderr.strip()[-800:]])
    return repo


def _ca_hash():
    proc = run("openssl x509 -in /etc/kubernetes/pki/ca.crt -pubkey -noout | "
               "openssl pkey -pubin -outform der | openssl dgst -sha256 -hex",
               timeout=30, check=True)
    m = re.search(r"([0-9a-fA-F]{64})", proc.stdout or "")
    if not m:
        raise ActionFailure("无法计算 CA 证书 hash")
    return "sha256:" + m.group(1).lower()


def _join_credentials(control_plane=False):
    token_proc = run(["kubeadm", "token", "create", "--ttl", "4h"], timeout=180, check=True)
    token = (token_proc.stdout or "").strip().splitlines()[-1].strip()
    cert_key = ""
    if control_plane:
        key_proc = run(["kubeadm", "init", "phase", "upload-certs", "--upload-certs"],
                       timeout=180, check=False)
        if key_proc.returncode != 0:
            raise ActionFailure("无法生成 control-plane certificate key",
                                errors=[key_proc.stderr.strip()[-800:]])
        lines = [x.strip() for x in (key_proc.stdout or "").splitlines() if x.strip()]
        for line in reversed(lines):
            if re.match(r"^[0-9a-fA-F]{64}$", line):
                cert_key = line
                break
        if not cert_key:
            raise ActionFailure("无法解析 certificate key")
    return {"token": token, "ca_hash": _ca_hash(), "certificate_key": cert_key}


def generate_join(params):
    ensure_root()
    return _join_credentials(control_plane=bool(params.get("control_plane")))


def join_cluster(params):
    ensure_root()
    version = params.get("version")
    endpoint = re.sub(r"^https?://", "", params.get("endpoint") or "")
    node_ip = params.get("node_ip")
    hostname = params.get("node_name") or socket.gethostname()
    control_plane = bool(params.get("control_plane"))
    if not (version and endpoint and node_ip and params.get("token") and params.get("ca_hash")):
        raise ActionFailure("join 参数不完整")
    _setup_kubelet_node_ip(node_ip, hostname)
    pause = params.get("pause_image")
    if pause:
        set_sandbox_image({"pause_image": pause})
        proc = run(["crictl", "--runtime-endpoint", CONTAINERD_SOCK, "pull", pause],
                   timeout=600, check=False)
        if proc.returncode != 0:
            raise ActionFailure("worker/control-plane 节点无法拉取 pause 镜像：%s" % pause,
                                errors=[proc.stderr.strip()[-800:]],
                                hint="检查镜像仓库/代理连通性，或使用 --image-repository 指定内网 Harbor")
    else:
        # 未提供 pause 时从镜像仓库推导
        repos = _image_repositories(params)
        image = _prepare_images({"version": version, "image_repositories": repos,
                                 "allow_upstream_fallback": False})
        pause = image["pause_image"]
    cmd = ["join", endpoint,
           "--token", params["token"],
           "--discovery-token-ca-cert-hash", params["ca_hash"],
           "--cri-socket", CONTAINERD_SOCK,
           "--node-name", hostname,
           "--v", "2"]
    if control_plane:
        if not params.get("certificate_key"):
            raise ActionFailure("control-plane join 缺少 certificate_key")
        cmd += ["--control-plane",
                "--certificate-key", params.get("certificate_key"),
                "--apiserver-advertise-address", node_ip]
    log("执行 kubeadm join（%s，%s）" % (endpoint, "control-plane" if control_plane else "worker"))
    proc = _kubeadm(cmd, timeout=1800, check=False)
    if proc.returncode != 0:
        raise ActionFailure("kubeadm join 失败",
                            errors=[proc.stderr.strip()[-1800:]],
                            hint="检查端点连通性、token 有效期和防火墙端口")
    return {"node_name": hostname, "control_plane": control_plane,
            "join_output": proc.stdout[-3000:]}


def preload_pause(params):
    image = params.get("pause_image")
    if image:
        run_ok(["crictl", "--runtime-endpoint", CONTAINERD_SOCK, "pull", image],
               timeout=900)
    return {"pause_image": image or ""}


def wait_ready(params):
    ensure_root()
    timeout = int(params.get("timeout") or 600)
    names = params.get("node_names") or []
    if not os.path.exists(KUBECONFIG):
        # worker 节点没有 admin.conf，由控制端在 master 上调用
        return {"skipped": True}
    _wait_api(timeout=min(timeout, 300))
    deadline = time.time() + timeout
    pending = set(names)
    last = ""
    while time.time() < deadline:
        proc = _kubectl(["get", "nodes", "--no-headers"], timeout=30, check=False)
        last = proc.stdout or proc.stderr
        ready = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "Ready":
                ready.add(parts[0])
        if not names and ready:
            return {"ready": sorted(ready)}
        if names and set(names) <= ready:
            return {"ready": sorted(ready)}
        time.sleep(5)
    raise ActionFailure("等待节点 Ready 超时", errors=[last[-1200:]],
                        hint="检查 CNI、kubelet 日志与镜像拉取状态")


def wait_coredns(params):
    ensure_root()
    timeout = int(params.get("timeout") or 300)
    proc = run(["kubectl", "--kubeconfig", KUBECONFIG, "-n", "kube-system",
                "rollout", "status", "deployment/coredns",
                "--timeout=%ds" % timeout], timeout=timeout + 60, check=False)
    if proc.returncode != 0:
        pods = run_ok(["kubectl", "--kubeconfig", KUBECONFIG, "-n", "kube-system",
                       "get", "pods", "-o", "wide"], timeout=60)
        raise ActionFailure("CoreDNS 未就绪",
                            errors=[(proc.stderr + "\n" + pods.stdout)[-1600:]],
                            hint="检查 CoreDNS Pod、CNI 和节点污点")
    return {"ready": True}


def untaint_master(params):
    ensure_root()
    hostname = params.get("node_name") or socket.gethostname()
    for taint in ("node-role.kubernetes.io/control-plane:NoSchedule-",
                  "node-role.kubernetes.io/master:NoSchedule-"):
        run_ok(["kubectl", "--kubeconfig", KUBECONFIG, "taint", "nodes", hostname, taint],
               timeout=60)
    return {"untainted": hostname}


def cluster_info(params):
    ensure_root()
    info = {"kubeconfig_exists": os.path.exists(KUBECONFIG)}
    if not info["kubeconfig_exists"]:
        return info
    info["server"] = ""
    for line in read_file(KUBECONFIG).splitlines():
        m = re.match(r"^\s*server:\s*(\S+)", line)
        if m:
            info["server"] = m.group(1)
            break
    proc = _kubectl(["version", "-o", "json"], timeout=30, check=False)
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
            info["version"] = data.get("serverVersion", {}).get("gitVersion", "")
            info["git_version"] = data.get("serverVersion", {}).get("gitVersion", "")
        except ValueError:
            pass
    proc = _kubectl(["get", "nodes", "-o", "json"], timeout=60, check=False)
    nodes = []
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
            for item in data.get("items", []):
                meta = item.get("metadata", {})
                status = item.get("status", {})
                labels = meta.get("labels", {}) or {}
                role = "master" if ("node-role.kubernetes.io/control-plane" in labels or
                                    "node-role.kubernetes.io/master" in labels) else "worker"
                ip = ""
                for addr in status.get("addresses", []):
                    if addr.get("type") == "InternalIP":
                        ip = addr.get("address", "")
                        break
                ready = False
                for cond in status.get("conditions", []):
                    if cond.get("type") == "Ready" and cond.get("status") == "True":
                        ready = True
                nodes.append({"name": meta.get("name", ""), "role": role, "ip": ip,
                              "ready": ready, "version": status.get("nodeInfo", {}).get("kubeletVersion", "")})
        except ValueError:
            pass
    info["nodes"] = nodes
    cni = ""
    cni_dir = "/etc/cni/net.d"
    if os.path.isdir(cni_dir):
        try:
            names = " ".join(os.listdir(cni_dir)).lower()
            if "calico" in names:
                cni = "calico"
            elif "flannel" in names:
                cni = "flannel"
        except OSError:
            pass
    info["cni"] = cni
    proc = _kubectl(["-n", "kube-system", "get", "configmap", "kubeadm-config",
                     "-o", "jsonpath={.data.ClusterConfiguration}"], timeout=30, check=False)
    if proc.returncode == 0:
        yaml_text = proc.stdout or ""
        m = re.search(r"(?m)^controlPlaneEndpoint:\s*(\S+)", yaml_text)
        if m:
            info["endpoint"] = m.group(1)
        m = re.search(r"(?m)^podSubnet:\s*(\S+)", yaml_text)
        if m:
            info["pod_cidr"] = m.group(1)
        m = re.search(r"(?m)^serviceSubnet:\s*(\S+)", yaml_text)
        if m:
            info["service_cidr"] = m.group(1)
        m = re.search(r"(?m)^imageRepository:\s*(\S+)", yaml_text)
        if m:
            info["image_repository"] = m.group(1).strip("'\"")
    info["admin_conf_b64"] = b64e(read_file(KUBECONFIG))
    repo = info.get("image_repository") or DEFAULT_IMAGE_REPOSITORY
    version = info.get("version") or ""
    info["pause_image"] = ""
    if version:
        try:
            images, pause = _image_list(version, repo)
            info["pause_image"] = pause
            info["images"] = images
        except ActionFailure as exc:
            log("读取 pause 镜像失败：%s" % exc)
    return info


def wait_node_ready(params):
    return wait_ready(params)


def drain_node(params):
    ensure_root()
    name = params.get("node_name")
    if not name:
        raise ActionFailure("缺少 node_name")
    proc = _kubectl(["drain", name, "--ignore-daemonsets", "--delete-emptydir-data",
                     "--force", "--timeout=300s"], timeout=360, check=False)
    return {"node": name, "ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-2000:]}


def uncordon_node(params):
    ensure_root()
    name = params.get("node_name")
    if not name:
        raise ActionFailure("缺少 node_name")
    _kubectl(["uncordon", name], timeout=120, check=False)
    return {"node": name}


def reset_node(params):
    ensure_root()
    force = bool(params.get("force", True))
    cmd = ["kubeadm", "reset", "-f"]
    if params.get("cri_socket", True):
        cmd += ["--cri-socket", CONTAINERD_SOCK]
    run_ok(cmd, timeout=600)
    # 清理 CNI / 网络设备 / 残留状态
    run_ok(["rm", "-rf", "/etc/cni/net.d", "/var/lib/etcd", "/var/lib/kubelet",
            "/etc/kubernetes", "/var/lib/kube-deploy"], timeout=120)
    run_ok(["ip", "link", "delete", "cni0"], timeout=30)
    run_ok(["ip", "link", "delete", "flannel.1"], timeout=30)
    run_ok(["systemctl", "disable", "--now", "kubelet"], timeout=60)
    run_ok(["systemctl", "disable", "--now", "haproxy"], timeout=60)
    run_ok(["systemctl", "disable", "--now", "keepalived"], timeout=60)
    return {"reset": True}
