# 物理机、虚拟机、公有云部署指南

KubeDeploy 的目标节点只要求：**Linux + systemd + 可 SSH + root/免密 sudo**。因此同一套工具可以用于物理服务器、VMware/KVM/OpenStack 虚拟机和公有云主机。差异集中在**网络地址、控制平面端点和高可用方式**，工具已提供对应模式。

## 1. 三种环境怎么选

| 环境 | 控制平面高可用 | 说明 |
|---|---|---|
| 物理服务器 | 内置 HAProxy + Keepalived VIP | 同一二层网络，交换机允许 VRRP/ARP；最成熟 |
| VMware / KVM / 私有云 | 内置 VIP（需验证） | VMware 需允许混杂模式/伪造传输；OpenStack 需 allowed-address-pairs/端口安全例外 |
| 公有云（AWS/Azure/阿里云/腾讯云/华为云/GCP 等） | **外部负载均衡（推荐/通常必须）** | 多数公有云不支持 VRRP/免费 ARP VIP；使用 NLB/CLB/SLB 做 TCP 6443 转发 |
| 单节点公有云 | 外部 LB（可选）或仅私网 | 私网端点最稳定；公网 kubeconfig 通过 LB 或堡垒机访问 |

## 2. 公有云 NAT/EIP：SSH 地址 ≠ 节点 IP

公有云主机的 SSH 入口通常是 EIP/NAT 公网 IP，而 Kubernetes 必须使用网卡上的私网 IP。工具支持：

~~~bash
# 单节点：显式指定私网 IP
./k8s-deploy single \
  --node 47.100.1.10 \
  --node-ip 172.16.0.10 \
  --version 1.34.6

# 单节点：也可以写成 公网=私网
./k8s-deploy single \
  --node 47.100.1.10=172.16.0.10 \
  --version 1.34.6

# 多节点：每个节点都可映射
./k8s-deploy cluster \
  --masters 47.100.1.11=172.16.0.11,47.100.1.12=172.16.0.12,47.100.1.13=172.16.0.13 \
  --workers 47.100.1.21=172.16.0.21,47.100.1.22=172.16.0.22 \
  --control-plane-endpoint k8s-api.example.com:6443 \
  --external-endpoint \
  --version 1.34.6
~~~

不写映射时，工具默认启用**自动私网 IP 探测**：

1. 如果 SSH 地址本身就是节点本地地址，直接用它；
2. 否则读取节点 default route 网卡的 IPv4，作为 Kubernetes 节点 IP；
3. 在日志中明确提示；
4. 多网卡/多私网 IP 场景建议显式写 `公网=私网`，或加 `--no-auto-node-ip` 强制要求显式映射。

> 不要把公网 IP 直接作为 kubelet/API Server 的 advertise address。公网 IP 通常不在网卡上，kubeadm 会失败。

## 3. 公有云外部负载均衡（推荐）

工具不会调用云厂商 API 创建 LB/安全组，需要预先准备：

1. 创建 **TCP 负载均衡**（L4/NLB/CLB/SLB；不要做 TLS 终止）。
2. 后端：所有 master 的 **私网 IP:6443**。
3. 健康检查：TCP 6443，间隔 5–10 秒。
4. 监听：TCP 6443（或企业自定义端口），转发到后端 6443。
5. 安全组：允许 LB 到 master 6443；允许节点之间所需端口。
6. 将 LB 的 DNS 名或 VIP 传给工具：

~~~bash
--control-plane-endpoint <lb-dns-or-vip>:6443 --external-endpoint
~~~

kubeadm 的 controlPlaneEndpoint 和证书 SAN 会使用该 LB 端点。Kubeconfig 里的 server 也是 LB，因此操作机可以不在 VPC 内。

> 优先使用 **VPC 内网 LB**，并确认节点能访问 LB 的监听地址。部分云的公网 LB 不支持从后端节点 hairpin 访问自身；这种情况下应使用内网 NLB 或额外的内网解析，避免 kubeadm 初始化/join 卡住。

### 云端新建集群顺序

1. 创建 VPC、子网、私网 IP、EIP（仅 SSH）、NAT 网关；
2. 创建 3 台 master、N 台 worker（建议 master 为奇数）；
3. 创建 NLB，后端为 master 私网 IP:6443（此时健康检查未通过是正常的，API 尚未安装）；
4. 配置安全组（见下一节）；
5. 运行上面的 cluster 命令；
6. 安装完成后通过 LB 访问 kubeconfig。

### 云端扩容 master

expand 会用现有 LB 端点加入新 master；新 master 加入后，**必须手工把新 master 私网 IP:6443 加入 LB 后端**，否则它不参与 API 负载。工具会在扩容时提示。

### 云端升级/降级

通过 LB 操作；LB 应配置健康检查，将正在升级的 master 自动摘除。升级仍需逐节点 drain。

## 4. 安全组/防火墙

宿主机的 firewalld/ufw 由工具按角色配置；**公有云安全组/NSG 必须单独放行**：

| 来源 | 协议/端口 | 用途 |
|---|---|---|
| 操作机/堡垒机 | TCP 22（或自定义 SSH） | SSH |
| LB 子网 | TCP 6443 | LB → kube-apiserver |
| master 之间 | TCP 6443, 2379-2380, 10257, 10259 | 控制平面/etcd |
| 所有节点之间 | TCP 10250 | kubelet |
| 所有节点之间 | TCP 179，UDP 4789，IP 协议 4 | Calico BGP/VXLAN/IPIP（默认 Calico） |
| 所有节点之间（可选 Flannel） | UDP 8472 | Flannel VXLAN |
| 所有节点之间（如启用内置 VIP） | VRRP/IP 112、TCP 16443 | Keepalived/HAProxy（公有云通常不适用） |
| 业务入口 | TCP NodePort 30000-32767（或自定义） | NodePort 服务 |
| 所有节点出站 | HTTPS 443 | 阿里云/DaoCloud/GitHub 镜像下载；也可用内网镜像 |

> 云安全组建议只允许 VPC 内网段互访；不要对 0.0.0.0/0 开放 2379/10250/179/8472。

## 5. 公网访问 kubeconfig

三种方式：

1. **外部 LB（推荐）**：`--control-plane-endpoint <公网LB>:6443`，kubeconfig 直接可用。
2. **堡垒机/VPN**：kubeconfig 使用私网端点，操作机通过 VPN 或 SSH 跳板访问。
3. **不要**直接把 master 6443 暴露到公网；必须配合安全组白名单。

单节点且没有 LB 时，kubeconfig 的控制平面端点默认是私网 IP，这是正常的；请通过堡垒机/VPN 使用，或给单节点也配置一个 TCP LB。

## 6. 虚拟机/物理机内置 VIP

适用于同一二层网络：

- 物理交换机允许 VRRP（IP 协议 112）和 VIP ARP；
- VMware：端口组安全策略允许 **Promiscuous Mode / Forged Transmits / MAC Address Changes**；
- OpenStack：给 VIP 配置 allowed-address-pairs，或关闭端口安全；确认 Neutron 允许 VRRP；
- 公有云：一般不要使用；改用 NLB/CLB/SLB，或云厂商 HAVIP/高可用虚拟 IP。工具在检测到 AWS/Azure/阿里云/腾讯云/华为云等环境且选择内置 VIP 时会告警。

## 7. 镜像与出网

- 没有公网 NAT 的节点无法下载 containerd、便携 Python、Calico/Flannel 镜像；
- 解决方式：
  - 给私有子网配置 NAT 网关；
  - 或使用企业内网镜像：`--package-repo-base`、`--image-repository`、`--flannel-image`；
  - 或预先准备离线介质（当前版本未包含完整离线 bundle 生成）。
- 容器镜像仓库如 Harbor 需要 TLS/认证时，应先在节点 containerd 配置好证书/凭据，再传入 `--image-repository`。

## 8. 云盘/etcd 性能

etcd 对磁盘 fsync 延迟敏感。避免使用低性能网络盘作为 `/var/lib/etcd`；建议：

- 本地 SSD/NVMe，或高 IOPS 云盘；
- 3 个 master 分布在不同可用区（如云厂商支持 etcd 跨 AZ 时）；
- 定期做 etcd 快照并复制到对象存储；工具升级时会自动做一次本地快照，但不替代企业备份。

## 9. MTU

Calico IPIP/VXLAN 和 Flannel VXLAN 都会根据节点默认网卡 MTU 自动调整。若云网络本身有额外封装（IPsec、GRE、Global Accelerator），请确认 underlay MTU，并在必要时：

- 调整云网络 MTU；或
- 通过 `--pod-iface` 指定承载 VXLAN 的网卡；或
- 使用企业 CNI 自定义 MTU；Flannel 可后置修改 ConfigMap，Calico 可调整 Felix MTU 配置。

## 10. 验收清单

- [ ] 所有节点私网 IP 唯一，能被 kubeadm 用于 advertise；
- [ ] SSH 地址与节点 IP 映射正确（或自动探测日志正确）；
- [ ] 安全组按上表放行；
- [ ] 外部 LB 后端/健康检查正确；
- [ ] NTP/chrony 同步；
- [ ] 云盘 IOPS 满足 etcd；
- [ ] `kubectl get nodes` 全部 Ready；
- [ ] 跨节点 Pod 通信、CoreDNS、NodePort/LB 业务访问；
- [ ] 升级前备份可读、可恢复；
- [ ] 不依赖单台 master 的公网/私网单点。
