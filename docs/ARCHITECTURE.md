# 架构与执行流程

```text
Operator (CLI, Python 3.8+)
  ├─ 本地 IP 判断
  ├─ 原生 OpenSSH（key / agent / 可选密码 askpass）
  ├─ 构建并上传 agent 包（tar.gz over SSH stdin）
  └─ 逐节点调用 Python agent（JSON over stdin）
        ├─ detect / preflight
        ├─ prepare（sysctl、swap、SELinux、防火墙）
        ├─ install_packages（阿里云 apt/yum 源）
        ├─ install_runtime（静态 containerd/runc + systemd）
        ├─ init / join（kubeadm CLI，避免 v1beta3/v1beta4 配置差异）
        ├─ HAProxy/Keepalived
        ├─ Flannel（kubectl apply）
        └─ backup / upgrade / restore
```

## 为什么使用 kubeadm CLI 而不是 YAML 配置

kubeadm 的配置 API 从 v1beta3 演进到 v1beta4，并在 1.37 移除了 v1beta3。使用稳定的 CLI 参数（`--kubernetes-version`、`--control-plane-endpoint`、`--cri-socket`、`--upload-certs` 等）可以避免为每个 minor 维护不同 YAML schema。需要额外 KubeletConfiguration 时通过 systemd drop-in 设置 node-ip。

## 单节点本地/远程判断

- 本机地址：直接使用当前 Python 进程调用 agent；
- 远程地址：SSH 上传 agent，在目标节点运行 `python3 -m kubedeploy.agent`；
- 目标无 Python 3.8+：自动下载便携 CPython 3.11（固定 SHA256）到 `/opt/kube-deploy/python`。

## SSH 地址与 Kubernetes 节点 IP

- `NodeSpec.address` 是操作机 SSH 使用的地址，可以是公网 EIP、堡垒机地址或主机名；
- `NodeSpec.node_ip` 是 kubelet/apiserver advertise 使用的私网 IP；
- CLI 语法 `SSH地址=私网IP` 显式映射；未指定时工具会在连接后选择 default route 网卡 IPv4；
- `ClusterSpec.endpoint` 始终使用 `k8s_ip` 或外部 LB，不把 NAT 公网地址传给 kubeadm；
- kubeconfig 中的 server 是集群端点，不是 SSH 地址；公网访问应通过外部 LB 或 VPN/堡垒机。

## 控制平面 HA

- 每个 master 安装 HAProxy，监听 `*:16443`，后端为所有 master 的 6443；
- Keepalived 通过单播 VRRP 维护 VIP；
- kubeadm 的 `controlPlaneEndpoint` 为 `VIP:16443`；
- 扩容 master 时刷新所有既有 master 的 HAProxy 后端和 Keepalived peer 列表。

公有云一般不允许 VRRP/免费 ARP VIP；此时跳过内置 HA，使用：

```text
--control-plane-endpoint <NLB/CLB>:6443 --external-endpoint
```

kubeadm 的端点/证书 SAN 指向云 LB，扩容 master 后需把新 master 私网 IP:6443 加入 LB 后端。

## 升级与回滚

升级：

1. 目标版本包可用性检查；
2. 每个控制平面：etcd 快照 + 控制平面配置归档；
3. 逐节点 drain；
4. 首个控制平面 `kubeadm upgrade apply`，其余 `kubeadm upgrade node`，再升级 kubelet；
5. worker 同样 drain + kubelet 升级；
6. uncordon，等待 Ready。

回滚：

1. 校验目标版本对应的备份和节点拓扑；
2. 停止所有 kubelet；
3. 降级软件包；
4. 将首个 master 的 etcd 快照分发到所有控制平面；
5. 恢复每个节点的控制平面归档；
6. 所有控制平面同时恢复 etcd 并启动，等待 quorum/API；
7. 启动 worker，等待 Ready。

回滚不是 kubeadm 逆向操作，数据会回到快照时间点。
