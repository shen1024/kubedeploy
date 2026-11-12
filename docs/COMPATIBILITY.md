# 兼容性矩阵与上游限制

## 1. 总览

| 组件 | 支持范围 |
|---|---|
| Kubernetes | v1.28.0 – v1.37.x |
| CPU | amd64 / x86_64、arm64 / aarch64 |
| 初始化系统 | systemd |
| 容器运行时 | containerd 2.1.9（静态二进制） |
| CNI | Calico v3.32.0（默认，IPIP）或 Flannel VXLAN v0.27.4 |
| 控制平面 HA | HAProxy + Keepalived（VIP）；公有云推荐外部 NLB/CLB/SLB |
| 运行环境 | 物理机、VMware/KVM/OpenStack、公有云 VM |

## 2. 发行版 / Kubernetes 组合

| 系统 | 1.28–1.30 | 1.31–1.34 | 1.35–1.37 |
|---|---|---|---|
| CentOS/RHEL 7 | 可用（EOL，需可用 OS 源） | 可用但高风险 | **不可用**：内核 3.10 无 cgroup v2 |
| CentOS/RHEL 8 | 可用 | 可用 | 需切换到 cgroup v2 并升级内核，默认配置不可用 |
| CentOS Stream/RHEL 9 | 可用 | 可用 | 可用（cgroup v2） |
| CentOS Stream/RHEL 10 | 可用 | 可用 | 可用（cgroup v2） |
| Ubuntu 20.04 | 可用 | 可用 | 默认 cgroup v1，内核 5.4；需操作系统层升级，默认不可用 |
| Ubuntu 22.04 | 可用 | 可用 | 可用（cgroup v2，内核 5.15） |
| Ubuntu 24.04 | 可用 | 可用 | 可用 |
| Ubuntu 26.04 | 可用 | 可用 | 可用 |
| Debian 11/12/13 | 可用 | 可用 | 可用（取决于 cgroup v2） |
| openEuler / Kylin / UOS | 需 `--allow-untested-os` | 同左 | 必须在 cgroup v2 上 |

“可用”表示安装路径和上游依赖满足；仍建议企业验收。Kubernetes 1.28–1.33 已过社区维护期，1.28 已 EOL。

## 3. cgroup v1 的硬限制

Kubernetes 上游从 1.35 起将 kubelet `failCgroupV1` 默认设为 true，kubeadm 的 SystemVerification 预检也会将 cgroup v1 判定为错误。Kubernetes 1.36 文档仍提供临时 `failCgroupV1: false` 覆盖，但 v1 支持计划在 1.38 移除，不建议企业采用。

因此：

- CentOS 7 的 3.10 内核只能使用 cgroup v1，**无法运行 1.35–1.37**；
- CentOS 8 默认 cgroup v1，需通过内核启动参数启用 unified hierarchy 并升级到 5.8+ 内核；
- Ubuntu 20.04 默认 cgroup v1（5.4 内核），同样需要操作系统层迁移；
- 工具不会通过忽略预检或修改 kubelet 特性门控来伪装支持。

## 4. 软件源

| 内容 | 默认 |
|---|---|
| kubeadm/kubelet/kubectl/CNI | `https://mirrors.aliyun.com/kubernetes-new/core/stable/v<minor>/{deb,rpm}` |
| 控制平面镜像 | `registry.aliyuncs.com/google_containers` |
| containerd/runc/便携 Python | `https://files.m.daocloud.io/github.com/...` |
| Flannel | `ghcr.m.daocloud.io/flannel-io/...` |

阿里云 1.37 源同步可能滞后。若国内源不可用：

1. 使用 `--package-repo-base` 指向企业内网同步源；
2. 或允许工具回退官方源（会显式告警）；
3. 或使用 `--no-upstream-fallback` 让部署快速失败，等待镜像同步。

## 5. 架构说明

- kubeadm/kubelet 包由软件源按架构提供；静态 containerd 为多架构。
- CNI/控制平面镜像需为多架构 manifest；阿里云与官方 registry 均提供。
- Flannel 的 VXLAN 包在混合架构集群中仍可工作；但所有节点必须能互通 UDP 8472。
- 控制平面节点可以混合 amd64/arm64；生产环境建议同构，以简化运行时/内核验证。

## 6. 运行环境

| 环境 | 支持方式 |
|---|---|
| 物理服务器 | 内置 HAProxy+Keepalived VIP，要求二层网络和 VRRP |
| VMware/KVM/OpenStack | 可用内置 VIP，但需验证混杂模式/伪造传输/allowed-address-pairs |
| AWS/Azure/阿里云/腾讯云/华为云/GCP | 推荐外部 NLB/CLB/SLB 监听 TCP 6443；用 `--control-plane-endpoint <LB>:6443 --external-endpoint` |
| 公有云 NAT/EIP | SSH 地址与 K8s 私网 IP 分离；支持 `公网=私网`、`--node-ip`、自动私网 IP 探测 |
| 单节点云主机 | 私网端点 + VPN/堡垒机，或为单节点配置 TCP LB |

详见 `docs/DEPLOYMENT-TARGETS.md`。

## 7. 已知未覆盖

- 外部 etcd（本工具的自动备份/回滚仅支持 kubeadm 堆叠 etcd）。
- 离线完整 bundle 制作（可使用内网镜像达到准离线）。
- 跨多个 minor 的自动连续升级（需逐次执行）。
- 非 x86/arm 架构（ppc64le/s390x）。
- 非 systemd 系统（Alpine、OpenWrt 等）。
