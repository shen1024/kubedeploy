"""全局常量与默认值。"""

from __future__ import annotations

APP_NAME = "kubedeploy"
VERSION = "1.2.8"

# Kubernetes 支持范围（次版本）
K8S_MIN_MINOR = 28
K8S_MAX_MINOR = 37
K8S_MINORS = tuple(range(K8S_MIN_MINOR, K8S_MAX_MINOR + 1))

# 目标节点默认目录
REMOTE_ROOT = "/opt/kube-deploy"
REMOTE_LIB = REMOTE_ROOT + "/lib"
REMOTE_PYTHON = REMOTE_ROOT + "/python"
REMOTE_RUN = REMOTE_ROOT + "/run"
AGENT_STATE_DIR = "/var/lib/kube-deploy"
AGENT_STATE_FILE = AGENT_STATE_DIR + "/state.json"
AGENT_BACKUP_DIR = AGENT_STATE_DIR + "/backups"
AGENT_LOG_FILE = "/var/log/kube-deploy/agent.log"
CONTAINERD_SOCK = "unix:///run/containerd/containerd.sock"

# 网络默认值（与 Flannel 默认网段保持一致）
DEFAULT_POD_CIDR = "10.244.0.0/16"
DEFAULT_SERVICE_CIDR = "10.96.0.0/16"
DEFAULT_CNI = "calico"
DEFAULT_CALICO_VERSION = "v3.32.0"
DEFAULT_CALICO_IMAGE_REPOSITORY = "docker.m.daocloud.io/calico"
DEFAULT_CALICO_ENCAPSULATION = "ipip"
DEFAULT_DNS_DOMAIN = "cluster.local"
DEFAULT_VIP_PORT = 16443
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_NODE_PORT_RANGE = "30000-32767"

# 国内镜像
DEFAULT_KUBE_REPO_BASE = "https://mirrors.aliyun.com/kubernetes-new/core/stable"
UPSTREAM_KUBE_REPO_BASE = "https://pkgs.k8s.io/core:/stable:/v{minor}"
DEFAULT_IMAGE_REPOSITORY = "registry.aliyuncs.com/google_containers"
FALLBACK_IMAGE_REPOSITORY = "registry.cn-hangzhou.aliyuncs.com/google_containers"
UPSTREAM_IMAGE_REPOSITORY = "registry.k8s.io"
DEFAULT_FLANNEL_IMAGE = "ghcr.m.daocloud.io/flannel-io/flannel:v0.27.4"
DEFAULT_FLANNEL_CNI_IMAGE = "ghcr.m.daocloud.io/flannel-io/flannel-cni-plugin:v1.8.0-flannel1"
DEFAULT_REGISTRY_MIRRORS = [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.net",
]

# 运行时版本。静态构建，避免依赖发行版 glibc / package repo。
DEFAULT_CONTAINERD_VERSION = "2.1.9"
DEFAULT_RUNC_VERSION = "1.3.4"

# 固定 SHA256（静态二进制，amd64/arm64）
CONTAINERD_SHA256 = {
    "2.1.9": {
        "amd64": "d52dec99dd13e63c6a24f292936c15f929ad826be1ce39d7a77a3d840b1a2299",
        "arm64": "f0e77d2bd29c6a4a463c593b3e9e39ce31b830ff572aba98ea544aa3b8f08be7",
    }
}
RUNC_SHA256 = {
    "1.3.4": {
        "amd64": "5966ca40b6187b30e33bfc299c5f1fe72e8c1aa01cf3fefdadf391668f47f103",
        "arm64": "d6dcab36d1b6af1b72c7f0662e5fcf446a291271ba6006532b95c4144e19d428",
    }
}

# 便携 Python（CentOS 7 等没有 Python 3.8+ 的节点）
PYTHON_VERSION = "3.11.10"
PYTHON_BUILD = "20241016"
PYTHON_SHA256 = {
    "amd64": "8b50a442b04724a24c1eebb65a36a0c0e833d35374dbdf9c9470d8a97b164cd9",
    "arm64": "803e49259280af0f5466d32829cd9d65a302b0226e424b3f0b261f9daf6aee8f",
}
PYTHON_ASSET = {
    "amd64": "cpython-3.11.10%2B20241016-x86_64-unknown-linux-gnu-install_only.tar.gz",
    "arm64": "cpython-3.11.10%2B20241016-aarch64-unknown-linux-gnu-install_only.tar.gz",
}
PYTHON_DOWNLOAD_MIRRORS = [
    "https://files.m.daocloud.io/github.com/astral-sh/python-build-standalone/releases/download/{build}/{asset}",
    "https://github.com/astral-sh/python-build-standalone/releases/download/{build}/{asset}",
]

RUNTIME_DOWNLOAD_MIRRORS = [
    "https://files.m.daocloud.io/github.com/{repo}/releases/download/{tag}/{asset}",
    "https://github.com/{repo}/releases/download/{tag}/{asset}",
]

# 必需的 Linux 内核模块 / sysctl
KERNEL_MODULES = ("overlay", "br_netfilter")
SYSCTL_VALUES = {
    "net.bridge.bridge-nf-call-iptables": "1",
    "net.bridge.bridge-nf-call-ip6tables": "1",
    "net.ipv4.ip_forward": "1",
    "vm.max_map_count": "262144",
    "fs.file-max": "1000000",
    "fs.inotify.max_user_instances": "8192",
    "fs.inotify.max_user_watches": "524288",
}

# 防火墙端口
MASTER_TCP_PORTS = ("6443", "2379-2380", "10250", "10257", "10259")
MASTER_UDP_PORTS = ("8472", "112")
WORKER_TCP_PORTS = ("10250",)
WORKER_UDP_PORTS = ("8472",)

# 支持的操作系统（用于告警/校验）
SUPPORTED_OS_FAMILIES = ("debian", "rhel")
SUPPORTED_UBUNTU = ("20.04", "22.04", "24.04", "26.04")
SUPPORTED_DEBIAN = ("11", "12", "13")
SUPPORTED_EL_MAJOR = (7, 8, 9, 10)
SUPPORTED_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

# 升级/回滚约束
MAX_MINOR_SKEW = 1
MIN_DISK_GB = 10
MIN_MEM_MB = 1700
RECOMMENDED_MEM_MB = 2048
