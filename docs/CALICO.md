# Calico CNI（默认）

KubeDeploy 1.2.0 起默认使用 Calico v3.32.0，不再默认安装 Flannel。

## 1. 兼容性

| 组件 | 版本 |
|---|---|
| Kubernetes | 1.34（当前目标）；Calico 3.32 官方验证 1.34–1.36 |
| Calico | v3.32.0 |
| 封装 | IPIP（默认）/ VXLAN / none（BGP） |
| CPU | amd64、arm64 |

## 2. 国内镜像

Calico 清单已随发布包分发（来源：Calico 官方 v3.32.0 清单，经 DaoCloud GitHub 国内代理获取），部署时不需要访问 GitHub。

默认镜像前缀：

~~~text
docker.m.daocloud.io/calico
~~~

实际镜像：

~~~text
docker.m.daocloud.io/calico/node:v3.32.0
docker.m.daocloud.io/calico/cni:v3.32.0
docker.m.daocloud.io/calico/kube-controllers:v3.32.0
~~~

自定义内网镜像：

~~~bash
--calico-image-repository harbor.example.com/calico
~~~

## 3. 部署

默认：

~~~bash
./k8s-deploy single --node 192.168.1.10 --version 1.34.6
~~~

显式指定：

~~~bash
./k8s-deploy single --node 192.168.1.10 --version 1.34.6 \
  --cni calico --calico-encapsulation ipip
~~~

多节点：

~~~bash
./k8s-deploy cluster \
  --masters 10.0.0.11,10.0.0.12,10.0.0.13 \
  --workers 10.0.0.21,10.0.0.22 \
  --vip 10.0.0.100 --vip-interface eth0 \
  --version 1.34.6
~~~

工具会：

1. 预拉取 Calico 镜像；
2. 渲染 Calico 清单：
   - Pod CIDR 写入 CALICO_IPV4POOL_CIDR；
   - 设置 IP_AUTODETECTION_METHOD=kubernetes-internal-ip；
   - 根据封装设置 IPIP/VXLAN；
   - 替换为国内镜像前缀；
3. kubectl apply；
4. 等待 calico-node 和 calico-kube-controllers 就绪。

## 4. 封装选择

| 参数 | 说明 | 适用 |
|---|---|---|
| ipip（默认） | IP 协议 4 隧道 | VMware/物理机/大多数 VPC；云安全组需放行协议 4 |
| vxlan | UDP 4789 隧道 | IPIP 被限制的环境 |
| none | 纯 BGP，无封装 | 同网段二层网络、可控路由；需 179/tcp 与 BGP 可达 |

单节点三种模式均可；跨节点 Pod 通信取决于封装和网络策略。

## 5. 端口/防火墙

Calico 需要：

- TCP 179（BGP）
- UDP 4789（VXLAN；IPIP 模式也会预放行）
- IP 协议 4（IPIP）
- Kubernetes 6443/2379-2380/10250/10257/10259

工具会尝试通过 firewalld/ufw 放行。公有云安全组仍需手工放行；IPIP 协议在安全组中通常要单独加“自定义协议 4”。

## 6. 从 Flannel 切换/清理 8472

如果之前部署过 Flannel，kubeadm reset 后仍可能残留：

- flannel.1 VXLAN 网卡（内核占用 8472/udp，ss 可能看不到进程）
- cni0 网桥
- /etc/cni/net.d/10-flannel.conflist

检查：

~~~bash
ip -d link show flannel.1
ss -lunp | grep 8472
~~~

手工清理：

~~~bash
ip link delete flannel.1 2>/dev/null || true
ip link delete cni0 2>/dev/null || true
rm -f /etc/cni/net.d/10-flannel.conflist
ss -lunp | grep 8472
~~~

1.2.0 在执行 Calico 的 prepare 阶段会自动做同样清理。

> Calico 不监听 8472，因此清理后不会再出现该端口。

## 7. 验证

~~~bash
kubectl get nodes -o wide
kubectl -n kube-system get pods -l k8s-app=calico-node -o wide
kubectl -n kube-system get pods -l k8s-app=calico-kube-controllers -o wide
kubectl -n kube-system logs -l k8s-app=calico-node --tail=100
~~~

节点 Ready 前 Calico 可能短暂 Pending，属正常；若超过 5 分钟：

~~~bash
kubectl -n kube-system describe pod -l k8s-app=calico-node
crictl images | grep calico
~~~

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| ImagePullBackOff | 检查 docker.m.daocloud.io/calico 连通性，或换内网仓库 |
| Calico 节点选错网卡 | 已设置为 kubernetes-internal-ip；检查 Node InternalIP |
| Pod 跨节点不通 | 检查封装模式、179/tcp、IP 协议 4/4789 |
| 8472 仍被占用 | 删除 flannel.1，见第 6 节 |
| 旧 Flannel 规则残留 | 在维护窗口清理 iptables/重启节点；不要直接覆盖生产规则 |
| 1.35+ 不启动 | 先解决 cgroup v2 上游限制 |

## 9. 版本来源

- Calico 版本：v3.32.0
- 清单文件：kubedeploy/resources/calico/v3.32.0/calico.yaml
- 清单下载源（国内代理）：
  https://files.m.daocloud.io/github.com/projectcalico/calico/raw/v3.32.0/manifests/calico.yaml
- 许可证：Apache-2.0（Calico 项目）
