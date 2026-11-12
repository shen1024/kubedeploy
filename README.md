# KubeDeploy Enterprise

企业级 Kubernetes 一键部署工具，覆盖 **Kubernetes v1.28 – v1.37**，面向国内网络环境，默认使用阿里云等国内镜像。支持 **amd64/x86_64** 与 **arm64/aarch64**，可用于**物理服务器、VMware/KVM/OpenStack 虚拟机和公有云主机**；通过静态 containerd + kubeadm 软件包实现跨发行版部署。

> 当前版本：1.2.8  
> 工具形态：单机 CLI + 远程 Python agent。CLI 可运行在 Linux/macOS/Windows；目标节点支持 systemd Linux。

---

## 1. 功能

| 编号 | 功能 | 命令 |
|---|---|---|
| 1 | 部署单节点集群，自动判断目标是本机还是远程 | `kubedeploy single --node <IP>`；云上可加 `--node-ip <私网IP>` |
| 2 | 部署多节点集群，用户指定 master / worker | `kubedeploy cluster --masters "公网IP=私网IP,..." --workers ...` |
| 3 | 已有集群扩容（可加 master / worker） | `kubedeploy expand --bootstrap <IP> ...` |
| 4 | 更新 Kubernetes 版本（滚动升级，自动备份） | `kubedeploy upgrade --bootstrap <IP> --to <版本>` |
| 5 | 降级 / 回滚（从升级前快照恢复） | `kubedeploy downgrade --bootstrap <IP> --to <版本>` |
| - | 交互式菜单 | `kubedeploy`（无参数） |
| - | 预检 / 状态 | `--preflight-only`、`kubedeploy status` |
| - | 清理单节点（危险） | `kubedeploy reset --node <IP>` |

交互菜单会依次引导输入版本、节点 IP、master/worker 角色、VIP、SSH 参数，并展示计算出的节点总数。

### 主要特性

- **不依赖 Docker**：安装静态 containerd 2.1、runc、kubeadm/kubelet/kubectl。
- **不挑系统包管理器**：同时支持 apt（Ubuntu/Debian）与 dnf/yum（CentOS/RHEL/Rocky/Alma/openEuler 等）。
- **自动软件源**：按 OS family/版本、amd64/arm64、K8s minor 自动生成 v1.34 仓库、apt arch、GPG key 和回退策略；不再手工写 repo。
- **系统 CA 兜底**：便携 Python 加载发行版 CA，失败时回退系统 curl，修复 RHEL 8 的证书校验失败。
- **节点名自动规范化**：localhost.localdomain/非法主机名自动使用 node-<IP>，并设置 kubelet --hostname-override。
- **不依赖目标节点 Python**：目标节点没有 Python 3.8+ 时，从国内 DaoCloud GitHub 代理下载便携 CPython 3.11（SHA256 固定校验）到 `/opt/kube-deploy/python`。
- **不依赖本机 Python**：`k8s-deploy` 启动器同样会引导便携 Python；已安装 Python 3.8+ 时直接使用。
- **CNI 默认 Calico v3.32.0**：内置官方清单，镜像走 `docker.m.daocloud.io/calico`；支持 IPIP/VXLAN/BGP，亦可 `--cni flannel`。
- **国内镜像优先**：
  - kubeadm/kubelet/kubectl：阿里云 `kubernetes-new` 软件源；
  - 控制平面镜像：`registry.aliyuncs.com/google_containers`；
  - runtime/便携 Python：`files.m.daocloud.io`；
  - Calico：`docker.m.daocloud.io/calico`；Flannel：`ghcr.m.daocloud.io`。
- **物理机/虚拟机/公有云**：内置 VIP（二层网络）或外部 NLB/CLB/SLB 两种控制平面入口；公有云 EIP/NAT 支持 SSH 地址与私网 IP 分离。
- **多 master HA**：内置 HAProxy + Keepalived（VIP），API 端点默认 `VIP:16443`，避免与 kube-apiserver 的 6443 冲突。
- **滚动升级**：控制平面 → 其他 master → worker，逐节点 drain/uncordon，版本对齐校验。
- **升级前自动备份**：etcd 快照 + `/etc/kubernetes` 配置归档，用于安全回滚。
- **可重复执行**：幂等准备、状态落盘、失败信息包含修复建议。

---

## 2. 快速开始

### 2.1 操作机

- Linux / macOS / Windows（Windows 建议只作远程操作机，不支持把 Windows 作为 Kubernetes 节点）。
- Python 3.8+；或直接使用 `./k8s-deploy` 启动器（Linux 下可自动安装便携 Python）。
- 能通过 SSH 访问目标节点；建议使用 SSH key 或 ssh-agent。
- 目标节点：root，或 SSH 用户具备 **免密 sudo**。

```bash
# 方式 A：源码目录直接运行（推荐先 dry-run）
chmod +x k8s-deploy install.sh
./k8s-deploy

# 方式 B：安装到 /usr/local/bin
sudo ./install.sh
kubedeploy --help

# 方式 C：pip 安装（需要 Python 3.8+）
pip install .
kubedeploy --help
```

Windows 操作机没有 POSIX 启动器，可直接运行：

```powershell
py -3 k8s-deploy.py --help
py -3 k8s-deploy.py single --node 10.0.0.11 --version 1.34.6 --ssh-key $HOME\.ssh\id_ed25519
```

Windows 控制端请使用 SSH 密钥/agent（不建议密码认证）；Windows 不能作为 Kubernetes 目标节点。

### 2.2 交互式一键部署

```bash
./k8s-deploy
```

菜单：

```text
1) 部署单节点集群
2) 部署多节点集群
3) 已有单节点集群扩容为多节点集群
4) 更新 Kubernetes 版本
5) 降级/回滚
6) 查看集群状态
0) 退出
```

### 2.3 单节点

本机 IP：

```bash
./k8s-deploy single --node 10.0.0.11 --version 1.34.6
```

远程 IP：

```bash
./k8s-deploy single --node 10.0.0.11 --version 1.34.6 \
  --ssh-user root --ssh-key ~/.ssh/id_ed25519
```

输入 IP 属于本机时直接本地执行；否则建立 SSH、上传 Python agent 并在目标节点执行。单节点部署完成后自动移除控制平面 `NoSchedule` 污点。

默认 CNI 是 **Calico v3.32.0**（IPIP），无需额外参数：

```bash
./k8s-deploy single --node 192.168.1.10 --version 1.34.6
```

如需 VXLAN 或旧 Flannel：

```bash
--calico-encapsulation vxlan
--cni flannel
```

### 2.4 公有云 NAT/EIP 与外部负载均衡

公有云常用“公网 EIP 登录、私网 IP 组集群”。节点表达式支持 `SSH地址=私网IP`：

```bash
# 3 master + 2 worker；公网 EIP 登录，私网 IP 作为 K8s 地址
./k8s-deploy cluster \
  --masters 47.100.1.11=172.16.0.11,47.100.1.12=172.16.0.12,47.100.1.13=172.16.0.13 \
  --workers 47.100.1.21=172.16.0.21,47.100.1.22=172.16.0.22 \
  --control-plane-endpoint k8s-api.example.com:6443 --external-endpoint \
  --version 1.34.6
```

不写映射时，工具会自动选择节点 default route 网卡的私网 IP（日志会明确提示）；多网卡场景建议显式映射或 `--no-auto-node-ip`。

公有云通常不支持 Keepalived/VRRP VIP，应使用云 NLB/CLB/SLB（TCP 6443，后端为 master 私网 IP）。完整步骤、安全组、云盘/MTU/堡垒机说明见 [云上/虚拟机/物理机部署指南](<docs/DEPLOYMENT-TARGETS.md>)。

### 2.5 多节点（3 master + 2 worker，内置 HA）

```bash
./k8s-deploy cluster \
  --masters 10.0.0.11,10.0.0.12,10.0.0.13 \
  --workers 10.0.0.21,10.0.0.22 \
  --vip 10.0.0.100 --vip-interface eth0 \
  --version 1.34.6
```

节点总数由 IP 数量自动计算。第一个 master 为初始化节点。

外部负载均衡：

```bash
./k8s-deploy cluster \
  --masters 10.0.0.11,10.0.0.12,10.0.0.13 \
  --control-plane-endpoint k8s-api.example.com:6443 \
  --external-endpoint --version 1.34.6
```

### 2.6 扩容

```bash
# 加 worker
./k8s-deploy expand --bootstrap 10.0.0.11 --workers 10.0.0.23,10.0.0.24

# 加 master + worker（已有 HA 集群）
./k8s-deploy expand --bootstrap 10.0.0.11 \
  --masters 10.0.0.14 --workers 10.0.0.25
```

没有 VIP/外部 LB 的旧单节点集群增加 master 时，API 端点仍是首 master IP，**不提供 API 故障切换**；工具会要求显式确认（或 `--allow-control-plane-without-ha`）。生产环境建议先完成稳定端点/HA 建设。

### 2.7 升级

```bash
./k8s-deploy upgrade --bootstrap 10.0.0.11 --to 1.35.1
```

升级前自动对每个控制平面执行 etcd 快照和 `/etc/kubernetes` 归档。支持同一 minor 的 patch 升级，以及相邻 minor 升级。**Kubernetes 不支持跨多个 minor 直接升级**；当前版本会拒绝超过一个 minor 的跳版。可逐次执行，或使用企业变更窗口分批升级。

### 2.8 降级 / 回滚

```bash
./k8s-deploy downgrade --bootstrap 10.0.0.11 --to 1.34.6
```

> **重要**：Kubernetes 官方不支持原地降级。本功能是**恢复升级前的 etcd 快照与静态 Pod 配置**，不是逆向执行 kubeadm。它要求该目标版本曾由本工具升级并成功创建备份；恢复会丢失升级后到回滚时刻之间写入 etcd 的数据。

必须通过交互确认或 `--confirm-data-loss`；`--yes` 不能替代数据丢失确认。

---

## 3. 兼容性

### 3.1 支持范围

- Kubernetes：**v1.28.x – v1.37.x**。可写 `1.34`（取软件源中最新 patch）或精确 `1.34.6`。
- CPU：**amd64/x86_64、arm64/aarch64**。
- 发行版：Ubuntu 20.04/22.04/24.04/26.04；CentOS/RHEL/Rocky/Alma/Oracle Linux 7/8/9/10；Debian 11/12/13；openEuler、Kylin、UOS 等 apt/dnf/yum + systemd 系统（需要 `--allow-untested-os` 放行未在矩阵中的版本）。
- 容器运行时：containerd（静态安装），systemd cgroup driver。
- 网络：Calico v3.32.0（默认，IPIP；官方验证 K8s 1.34–1.36）；可选用 Flannel VXLAN。Pod CIDR 默认 `10.244.0.0/16`，Service CIDR 默认 `10.96.0.0/16`，且不能与 `192.168.0.0/16` 重叠。Calico 3.32 未覆盖 1.37；1.37 建议先使用 Flannel 或替换经企业验证的 Calico 清单。

### 3.2 上游硬限制（必须阅读）

| Kubernetes | cgroup 要求 | 结论 |
|---|---|---|
| 1.28 – 1.34 | cgroup v1 可用（1.31 起维护模式） | CentOS 7 / Ubuntu 20.04 默认配置可尝试 |
| 1.35 – 1.37 | **必须 cgroup v2**；kubelet `failCgroupV1` 默认 true，kubeadm 预检也会报错 | CentOS 7/8 与默认 cgroup v1 的 Ubuntu 20.04 **不能直接部署** |

因此“CentOS 7 + Kubernetes 1.37”在技术上不可行；这是 Kubernetes 上游限制，不是镜像或工具问题。工具会在预检/升级阶段明确拦截，不会伪装成功。升级也不能从 1.34 直接跳到 1.37：必须逐 minor；而 RHEL/CentOS 8 的 cgroup v1 连 1.35 都无法运行。建议：
- CentOS 7：使用 1.28–1.34，并规划迁移；
- 需要 1.35+：使用 CentOS Stream 9/10、Ubuntu 22.04+，或先在旧系统启用 cgroup v2 + 升级内核。

其他告警：Kubernetes 1.28–1.33 已过社区维护期；CentOS 7 已 EOL；这些组合仅建议存量/测试环境。

### 3.3 自动配置软件源（按系统版本 / 架构 / K8s 版本）

工具不再让用户手工写 repo 文件。每次安装前会：

1. 读取 `/etc/os-release`、包管理器、CPU 架构和目标 Kubernetes 版本；
2. 生成正确的仓库路径，例如 RHEL 8.4 x86_64 + Kubernetes 1.34.6：
   `https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm`；
3. 自动写入 `/etc/yum.repos.d/kubernetes.repo` 或
   `/etc/apt/sources.list.d/kube-deploy-kubernetes.list`；
4. 自动导入 GPG key；
5. apt 会写入 `arch=amd64` 或 `arch=arm64`；
6. 探测仓库元数据，国内源不可用时按配置回退官方源。

> 修复了早期版本把 `v1.34` 误拼成 `v34` 的问题；同时使用系统 CA + curl 回退，解决 RHEL 8.4 便携 Python 出现的
> `CERTIFICATE_VERIFY_FAILED`。

严格国内源/内网环境：

```bash
--no-upstream-fallback
# 或指定企业内网同步仓库；支持模板占位符
--package-repo-base 'https://mirror.example.com/k8s/{os_id}/{os_version}/{arch}'
```

模板占位符：`{minor}`（1.34）、`{minor_num}`（34）、`{arch}`、`{pkg_mgr}`、`{os_id}`、`{os_version}`、`{os_major}`、`{arch_raw}`。如果没有写版本目录，工具会自动追加 `/v1.34`。

镜像仓库也支持通过 `--image-repository`、`--flannel-image`、`--flannel-cni-image` 指向企业 Harbor/镜像代理。详见 [软件源自动配置与排错](<docs/REPOSITORIES.md>)。

### 3.4 主机名自动规范化

RHEL/CentOS 默认的 `localhost.localdomain` 不适合作为 Kubernetes 节点名。工具现在会：

- 使用合法主机名；
- 对 `localhost`、非法字符、空主机名自动生成 `node-<节点IP>`；
- 同时设置 kubeadm `--node-name` 和 kubelet `--hostname-override`；
- 在部署日志中明确提示替换结果。

已有集群升级/扩容时优先使用 API 中已有的节点名，不会重命名运行中的节点。

### 3.5 物理机 / 虚拟机 / 公有云

同一套工具覆盖三类运行环境，差异在控制平面端点和高可用方式：

| 环境 | 推荐 HA | 关键点 |
|---|---|---|
| 物理服务器 | 内置 VIP | 同一二层网络、允许 VRRP/ARP |
| VMware/KVM/私有云 | 内置 VIP（需验证） | VMware 混杂模式/伪造传输；OpenStack allowed-address-pairs |
| 公有云 | **云 NLB/CLB/SLB** | 用 `--control-plane-endpoint ... --external-endpoint`；NAT/EIP 用 `公网=私网` 映射 |
| 单节点云主机 | 私网或外部 LB | 无 LB 时 kubeconfig 使用私网地址，通过 VPN/堡垒机访问 |

详细操作、安全组、负载均衡健康检查、云盘/MTU、扩容后 LB 后端更新见 [部署目标指南](<docs/DEPLOYMENT-TARGETS.md>)。

---

## 4. SSH 与权限

- 默认 `root`；非 root 用户必须配置免密 sudo（`NOPASSWD`）。
- 操作机建议 OpenSSH 7.6+（`accept-new`）；旧版 SSH 请预分发 known_hosts 并使用 `--ssh-strict-host-key-checking yes`。
- 公有云 NAT/EIP：SSH 可用公网 IP，Kubernetes 节点 IP 必须是节点本地私网 IP；工具支持 `公网=私网` 映射、`--node-ip` 和自动私网 IP 探测。
- 推荐 SSH key：
  ```bash
  ssh-copy-id -i ~/.ssh/id_ed25519.pub root@10.0.0.11
  ```
- 密码认证：Linux/macOS 操作机使用 OpenSSH `SSH_ASKPASS`，不写入命令行/状态文件；**Windows 控制端不建议密码认证**，请使用 key/agent。
- 默认主机密钥策略 `accept-new`，首次连接会写入 known_hosts。生产环境可预先分发 known_hosts 并使用 `--ssh-strict-host-key-checking yes`。
- 远程 agent 安装到 `/opt/kube-deploy`；状态/备份在 `/var/lib/kube-deploy`。

---

## 5. 网络与端口

| 节点 | TCP | UDP/协议 |
|---|---|---|
| master | 6443, 2379-2380, 10250, 10257, 10259, 16443（内置 HA） | 112（VRRP，仅内置 HA） |
| worker | 10250 | - |
| Calico 所有节点 | 179（BGP） | 4789/udp（VXLAN）、IP 协议 4（IPIP） |
| Flannel 所有节点 | - | 8472/udp |
| 所有节点 | NodePort 30000-32767（可通过 `--node-port-range` 修改） | - |

工具会在 firewalld/ufw 处于活动状态时按 CNI 放行端口；不会关闭防火墙。**公有云安全组/NSG 不会被工具修改**，需按“来源/端口”单独放行；不要对公网开放 2379/10250/8472/179。

Calico 的清单元信息、镜像与排错见 [Calico CNI 说明](<docs/CALICO.md>)。

VIP 方案：
- Keepalived 为 master 提供 VIP；
- HAProxy 监听 `0.0.0.0:16443`，转发到所有 master 的 6443；
- kubeadm 端点使用 `VIP:16443`，避免与 API Server 6443 冲突。

多 master 建议奇数（1/3/5）以满足堆叠 etcd 多数派。

---

## 6. 状态、日志与备份

操作机（可用 `KUBEDEPLOY_HOME` 指定其他目录）：

```text
~/.kube/kube-deploy/
├── clusters/<name>.json       # 集群清单、节点、备份索引
├── kubeconfigs/<name>.conf    # 集群 kubeconfig（0600）
├── logs/                      # 操作日志
```

目标节点：

```text
/opt/kube-deploy/              # agent 代码/便携 Python
/var/lib/kube-deploy/state.json
/var/lib/kube-deploy/backups/<backup-id>/<node>/
├── etcd-snapshot.db
├── control-plane.tgz
└── metadata.json
```

备份默认只保存在各自控制平面节点；升级后可另外从节点归档到企业备份系统。etcd 快照在回滚时会从首个 master 传输到其他控制平面（单次上限 512 MB），大型 etcd 建议配置企业对象存储/手工分发。

---

## 7. 安全设计

- 所有节点侧操作要求 root（或免密 sudo）。
- SSH 密码不写入状态文件、日志和子进程 argv；使用 0600 askpass 临时文件。
- 下载文件进行 SHA256 校验；固定 containerd/runc/便携 Python 版本与校验值。
- Kubernetes 包使用 apt/dnf 的 GPG 校验；回退官方源会显式告警。
- 不关闭 SELinux；默认切换为 permissive（可在 `--selinux enforcing` 下保留，需自行验证策略）。
- 不关闭防火墙，只按角色放行端口。
- kubeconfig 0600。
- **不自动执行 `kubeadm reset`**；失败时保留现场便于排查，重试前需要明确清理。

---

## 8. 前置检查与 dry-run

```bash
# 打印清单和目标，不连接/修改节点
./k8s-deploy cluster ... --dry-run

# 连接节点做完整预检，但不安装
./k8s-deploy single --node 10.0.0.11 --version 1.34.6 --preflight-only
```

预检包括：OS/架构、systemd、cgroup 版本、内核、root、swap、内存/磁盘、端口、已有集群、时间同步、包管理器、Docker 冲突等。

---

## 9. 已验证范围

本仓库包含 **43 个自动化测试**，覆盖：

- 版本解析、软件源版本选择、升级路径校验；
- 软件源按 K8s minor、OS、包管理器、CPU 架构生成（含 v1.34 路径防回归）；
- 多 master/VIP 校验、重复节点拒绝；
- 公有云 `SSH地址=私网IP` 映射、自动私网 IP 解析、VIP/节点 IP 冲突；
- localhost/非法主机名规范化、kubelet hostname-override；
- 云环境 VIP 告警、现有集群 SSH 地址保持；
- containerd v2/v3 配置补丁；
- Keepalived/HAProxy 配置生成、VRRP 稳定性；
- Flannel 清单渲染、etcd 静态 Pod 参数解析；
- 单节点/多 master 部署、扩容、升级、降级恢复顺序的 mock 编排；
- agent 包构建、CLI dry-run/help。

未在真实多发行版 × 多架构 × 全版本矩阵上执行端到端集群测试；上线前请在企业实验环境按《兼容性矩阵》完成验收。工具不会把未验证组合说成已验证。

---

## 10. 常见问题

**Q：已经部署成功，再次运行 `single` 报 8472/udp 被占用？**  
这是重复安装检测，不是新版本退化。8472 是 Flannel 正在使用。1.1.4 起会先检查已有集群：
- 集群健康：跳过重复安装，刷新 kubeconfig 并提示现有集群可用；
- 有配置但 API 不可用：停止并提示检查/reset；
- reset 后残留 Flannel：错误信息会提示清理残留容器。

已有集群直接用 kubeconfig 即可，不需要再次执行部署。

**Q：部署成功，但 `kubectl` 报 `localhost:8080 connection refused`？**  
这是 kubectl 没读到 kubeconfig，不是集群故障。工具把 kubeconfig 保存在
`~/.kube/kube-deploy/kubeconfigs/`；1.1.3 起如果 `~/.kube/config` 不存在，
会自动复制一份默认配置。已有部署可手工执行：

```bash
cp /root/.kube/kube-deploy/kubeconfigs/k8s-192-168-1-10.conf /root/.kube/config
chmod 600 /root/.kube/config
kubectl get pods -A
```

**Q：部署成功，但 kubectl 报 `x509: certificate signed by unknown authority`？**  
通常是用到了旧集群遗留的 `~/.kube/config`。先用集群专用 kubeconfig：

~~~bash
KUBECONFIG=/root/.kube/kube-deploy/kubeconfigs/k8s-192-168-1-10.conf kubectl get pods -A
~~~

1.2.2 起，如果默认 kubeconfig 的 server 与本次集群 endpoint 相同，会先备份
`~/.kube/config` 再更新；server 不同的默认配置仍然不覆盖。也可以手工替换：

~~~bash
cp /root/.kube/config /root/.kube/config.bak
install -m 600 /root/.kube/kube-deploy/kubeconfigs/k8s-192-168-1-10.conf /root/.kube/config
kubectl get pods -A
~~~

**Q：`kubeadm init` 报 `unknown flag: --timeout`？**  
1.1.2 已移除该参数。Kubernetes 1.34 的 `kubeadm init` 不支持 `--timeout`，子进程超时由工具自身控制。

**Q：升级到 1.35 时 containerd 启动失败，集群节点 NotReady？**  
1.2.7 已修复 sandbox_image 被写到错误 TOML 段的问题，并在修改失败时自动恢复
`/etc/containerd/config.toml.kubedeploy-*.bak`。旧版本已经中断时，先恢复最近备份：

~~~bash
ls -lt /etc/containerd/config.toml*
cp -a /etc/containerd/config.toml.kubedeploy-<时间>.bak /etc/containerd/config.toml
systemctl restart containerd
systemctl restart kubelet
~~~

不要执行 `kubeadm reset`，控制平面数据还在。

**Q：Ubuntu 报 `Could not get lock /var/lib/dpkg/lock-frontend`？**  
常见于 `unattended-upgrades` 正在运行。1.2.5 起 apt-get 使用
`DPkg::Lock::Timeout=600` 自动等待，最多 10 分钟。仍失败时查看：

~~~bash
ps -ef | grep -E 'unattended|apt|dpkg'
systemctl status unattended-upgrades
~~~

不要在 apt/dpkg 运行时强制删除锁文件。

**Q：Ubuntu 报 `Couldn't create temporary file /tmp/apt.conf...`？**  
这是 `/tmp` 满、权限或挂载异常，不是 apt 识别错误。1.2.3 起 APT 使用
`/var/tmp`（`Dir::Temp` + `TMPDIR`）；1.2.4 起 prepare 会自动把
`/tmp`、`/var/tmp` 修正为 `1777`（sticky）。仍失败时执行：

~~~bash
df -h /tmp /var/tmp
ls -ld /tmp /var/tmp
find /tmp -maxdepth 1 -type f -printf '%s %p\n' | sort -n | tail
~~~

必要时清理 `/tmp` 并确保 `chmod 1777 /tmp`。

**Q：RHEL 8.4 报 `CERTIFICATE_VERIFY_FAILED`，或 URL 出现 `v34`？**  
1.1.0 已修复：仓库目录统一为 `v1.34`；便携 Python 会加载系统 CA，失败时回退系统 `curl`。详见 [软件源自动配置与排错](<docs/REPOSITORIES.md>)。

**Q：软件源已选中，但 dnf 报 `No matching Packages to list`？**  
1.1.1 已修复：仓库文件里的 `exclude=kubelet...` 也会影响 `dnf list`，导致候选包被隐藏。现在查询候选包时同样使用 `--disableexcludes=kubernetes`；安装/降级命令也保持一致。

**Q：软件源需要手工配置吗？**  
不需要。工具根据 OS family/版本、apt/dnf/yum、amd64/arm64 和目标 K8s 版本自动生成仓库、导入 GPG key 并探测可用性。

**Q：主机名是 `localhost.localdomain` 怎么办？**  
工具会自动使用 `node-<IP>` 作为 Kubernetes 节点名，并设置 kubeadm `--node-name` 与 kubelet `--hostname-override`；已有集群升级时优先保留 API 中已有节点名。

**Q：为什么 1.37 安装时提示国内源缺失？**  
阿里云同步有延迟。可等待同步、配置企业镜像，或临时允许官方源回退。工具不会静默使用官方源。

**Q：CentOS 7 上没有 Python 3？**  
启动器/agent 会从 DaoCloud 代理下载便携 CPython 3.11。但 CentOS 7 仍需可用的 OS 软件源来安装 conntrack/socat 等依赖；EOL 系统建议切换 vault 或内部镜像。

**Q：多 master 没有 VIP？**  
工具默认阻止，除非显式 `--allow-control-plane-without-ha`。没有稳定端点时新增 master 不提供 API 故障切换。

**Q：升级失败怎么办？**  
升级前已保存 etcd 快照和控制平面归档。使用 `kubedeploy downgrade --bootstrap <IP> --to <升级前版本>` 恢复；会丢失备份时间点之后的数据。

**Q：安装中途失败，如何清理单节点后重试？**  
先保留现场排查，再执行：

```bash
kubedeploy reset --node 10.0.0.11
# 非交互/已确认：
kubedeploy reset --node 10.0.0.11 --force
```

该命令会删除该节点的 `/etc/kubernetes`、kubelet/etcd 数据、CNI 配置和 `/var/lib/kube-deploy`，但保留 containerd 与软件包。多节点环境需逐台确认后执行。

**Q：能原地降级 1.35 → 1.34 吗？**  
不能以“逆向 kubeadm”的方式安全降级。工具采用快照恢复；没有对应升级前备份时会拒绝。

**Q：支持离线安装吗？**  
当前版本以在线国内镜像为主；可通过企业内网镜像（`--package-repo-base`、`--image-repository`、`--flannel-image`）实现准离线/内网交付。完整离线包制作未包含在本版本中。

---

## 11. 目录

```text
kubedeploy/
├── k8s-deploy                 # POSIX 启动器（带便携 Python 引导）
├── k8s-deploy.py              # Python 入口
├── install.sh
├── kubedeploy/                # CLI + 远程 agent
├── tests/
├── docs/
└── README.md
```

许可证：Apache-2.0。
