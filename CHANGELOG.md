# 变更记录

## 1.2.8（2026-11-12）

### 修复

- 部分 containerd 2.1 默认配置没有显式
  `[plugins.'io.containerd.cri.v1.images']` 表，1.2.7 会拒绝升级。
  1.2.8 在该表不存在时创建它并写入 `sandbox_image`；
  如果只存在 `...images'.registry` 子表，则在子表之前插入父表。
- 仍保留 1.2.7 的自动备份恢复和“先验证镜像、后升级 kubeadm”顺序。

### 验证

- 43 个自动化测试通过。

## 1.2.7（2026-11-12）

### 修复

- 修复 containerd 2.1 配置中没有 `sandbox_image` 时，旧逻辑把该键
  追加到文件末尾，落入最后一个 TOML table，导致 containerd 启动失败。
  现在插入到 `[plugins.'io.containerd.cri.v1.images']`（或旧版 CRI 段）中。
- `set_sandbox_image` 修改配置后会验证 containerd；启动失败时自动恢复
  `.kubedeploy-*.bak` 备份并输出 journal。
- 升级时先用当前 kubeadm 预拉取目标镜像/验证 sandbox 配置，成功后才升级
  kubeadm，避免留下“kubeadm 1.35 + 控制平面 1.34 + containerd 坏配置”的半升级状态。

### 验证

- 42 个自动化测试通过。

## 1.2.6（2026-11-12）

### 改进

- 升级前先校验 minor 跨度和 cgroup 要求，再查询软件源，避免对
  `1.34 -> 1.37` 这类请求先刷新 v1.37 仓库后才报错。
- 当目标为 1.35+ 且现存节点是 cgroup v1 时，直接拒绝并说明：
  RHEL/CentOS 8、默认 Ubuntu 20.04 无法运行 Kubernetes 1.35+。
- 跨多个 minor 时输出逐级升级计划（例如 1.34 -> 1.35 -> 1.36 -> 1.37）。

## 1.2.5（2026-11-12）

### 修复

- Ubuntu 的 `unattended-upgrades` 会长时间持有
  `/var/lib/dpkg/lock-frontend`；此前 apt 立即失败。
- 所有 apt-get 安装命令现在加 `-o DPkg::Lock::Timeout=600`，
  最多等待 10 分钟；Kubernetes 组件安装也统一走 `_apt_cmd` 和
  `TMPDIR=/var/tmp`。

### 验证

- 41 个自动化测试通过。

## 1.2.4（2026-11-12）

### 修复

- Ubuntu 云镜像的 `/tmp` 常见权限为 `0755`，而 `apt-key` 会降权到
  `_apt` 用户，无法创建 `/tmp/apt.conf.*`。prepare 阶段现在自动把
  `/tmp`、`/var/tmp` 修正为 `1777`（sticky），并记录变更。
- 保留 1.2.3 的 `Dir::Temp=/var/tmp` 与 `TMPDIR` 兜底。

### 验证

- 41 个自动化测试通过。

## 1.2.3（2026-11-11）

### 修复

- Ubuntu 上 `apt-key` 报
  `Couldn't create temporary file /tmp/apt.conf.*`：
  - apt-get 统一加 `-o Dir::Temp=/var/tmp`；
  - apt/gpg 子进程设置 `TMPDIR=/var/tmp`；
  - GPG key 临时文件改到 `/var/tmp`；
  - 预检检查 `/tmp` 与 `/var/tmp` 可写性、`/tmp` sticky 位。
- 这是 Ubuntu 的临时目录问题，不是 apt/dnf 识别错误；RHEL 路径不变。

### 验证

- 41 个自动化测试通过。

## 1.2.2（2026-11-11）

### 修复

- 默认 `~/.kube/config` 已存在且与本次集群 endpoint 相同时，备份旧文件并
  更新，而不是直接跳过。修复“集群部署成功但 kubectl 使用旧 CA 报
  x509 certificate signed by unknown authority”的问题。
- endpoint 不同的默认 kubeconfig 仍然不覆盖，避免破坏其他集群。
- 延长 `kubeadm token create`/`upload-certs` 超时，避免慢速节点上生成
  join 凭据超时。

### 说明

- 40 个自动化测试通过。

## 1.2.1（2026-11-11）

### 修复

- 修复 DaoCloud 代理返回的 Calico YAML 缺少 `---` 文档分隔符，导致
  `kubectl apply` 把 ConfigMap/RBAC/Deployment 合并解析，
  报 `unknown field data/roleRef/rules` 的错误。渲染时按 `# Source:`
  自动补齐文档分隔符。
- 增加回归测试：渲染后的 Calico 清单必须包含多个 YAML 文档。
- 已有控制平面但 CNI/节点未 Ready 时，重新执行 single 会跳过 init，
  仅补装 CNI，便于修复本次中断。

### 验证

- 40 个自动化测试通过。

## 1.2.0（2026-11-11）

### 新增

- 默认 CNI 切换为 **Calico v3.32.0**（Kubernetes 1.34 已验证版本范围）。
- 内置 Calico 官方清单（从 DaoCloud GitHub 国内代理下载并随包分发），
  默认镜像前缀 `docker.m.daocloud.io/calico`，无需部署时访问 GitHub。
- 支持 `--cni calico|flannel`，Calico 默认 `ipip`，也可选
  `vxlan` 或 `none`（BGP）。
- Calico 节点 IP 自动使用 Kubernetes InternalIP；IPPool CIDR 自动与
  `--pod-cidr` 对齐。
- 切换到 Calico 前自动清理 reset 后残留的 `flannel.1`、`cni0` 和
  `10-flannel.conflist`，释放 8472/udp。
- 防火墙按 CNI 放行：Calico 179/tcp、4789/udp、IPIP 协议 4；
  Flannel 8472/udp。
- 对齐参考 playbook：
  - Service CIDR 默认 `10.96.0.0/16`；
  - 内置 HAProxy 端口默认 `16443`；
  - 默认时区 `Asia/Shanghai`；
  - 基础依赖增加 ipset、ipvsadm、nfs-common/nfs-utils、bash-completion；
  - 内核参数增加 `vm.max_map_count`、`fs.file-max`；
  - master 写入 `/etc/profile.d/kubectl.sh`（补全 + KUBECONFIG）；
  - 禁止 Pod/Service CIDR 与 `192.168.0.0/16` 重叠。

### 说明

- 默认命令现在使用 Calico；需要旧行为显式加 `--cni flannel`。
- 40 个自动化测试通过，新增 Calico 清单渲染与镜像替换测试。

## 1.1.4（2026-11-11）

### 新增/修复

- 重复执行 `single` 时，如果检测到已有健康单节点集群，不再进入预检/安装，
  改为刷新 kubeconfig、写入本地状态并明确提示“现有集群可用”。
- 如果节点上有 Kubernetes 配置但 API 不可用，停止并提示先检查或 reset，
  不再以“8472 端口占用”作为唯一错误。
- 8472/udp 冲突错误增加提示：可能是已有集群，或 reset 后残留 Flannel；
  避免用户误以为新版本比旧版本差。
- 已有集群不会被重复初始化覆盖；需要重装必须显式 reset。

## 1.1.3（2026-11-11）

### 新增

- 首次部署成功后，如果操作机不存在 `~/.kube/config`，自动写入一份
  0600 的默认 kubeconfig，使 `kubectl get nodes`、`kubectl get pods -A`
  可以直接使用。
- 已存在且非空的 `~/.kube/config` 不会被覆盖，避免破坏操作机上的其他集群；
  工具会继续提示集群专用 kubeconfig 路径和 `export KUBECONFIG=...`。
- `status`/扩容/升级刷新 kubeconfig 时，如果默认 kubeconfig 缺失也会补齐。

### 说明

- 本次只解决“kubectl 默认连 localhost:8080”的 UX 问题；集群本身在
  1.1.2 已部署成功。

## 1.1.2（2026-11-11）

### 修复

- 移除 `kubeadm init` 不支持的 `--timeout 15m` 参数。该参数在
  Kubernetes 1.34 上直接报 `unknown flag: --timeout`，导致镜像已拉取、
  但控制平面初始化无法开始。
- 将 init 命令构造抽成 `_init_command`，新增回归测试，防止以后重新加入
  不存在的 kubeadm 参数。

### 验证

- 38 个自动化测试通过。

## 1.1.1（2026-11-11）

### 修复

- 修复 RPM 仓库的 `exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni`
  同时影响 `dnf list` 查询，导致软件源已经被正确选中、元数据也刷新成功，
  但 `dnf --showduplicates list kubeadm` 返回
  `No matching Packages to list` 的问题。
- 查询候选包时统一加 `--disableexcludes=kubernetes`；安装/降级命令保持同样处理。

### 验证

- 37 个自动化测试通过，新增 dnf 排除项回归测试。

## 1.1.0（2026-11-11）

### 修复

- 修复 Kubernetes 软件源 URL 把 `v1.34` 误写成 `v34` 的严重问题。
- 修复 RHEL/CentOS 8 上便携 Python 的
  `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`：
  - 显式加载发行版系统 CA；
  - Python urllib 失败时回退系统 curl。
- 修复本地安装日志前缀固定为 `localhost`，现在显示实际节点地址。
- 修复 RHEL 8.4 `localhost.localdomain`/非法主机名导致 kubeadm 节点名风险：
  - 自动规范化为 `node-<IP>`；
  - 同时设置 kubelet `--hostname-override`；
  - 已有集群优先保留 API 节点名。
- 修复错误提示使用不存在的 `--kube-repo-base`，统一为 `--package-repo-base`。

### 新增

- 独立的软件源规划模块 `kubedeploy/repository.py`：
  - 按 K8s minor、OS、包管理器、CPU 架构生成仓库；
  - apt 自动写 `arch=amd64/arm64`；
  - 支持企业镜像模板占位符；
  - RPM 自动写 GPG key、SSL 校验和 exclude。
- 软件源探测结果输出到部署日志（来源、URL、架构）。
- `docs/REPOSITORIES.md` 软件源自动配置与排错指南。
- 新增软件源、主机名规范化相关测试。

### 调整

- 版本号：1.0.0 → 1.1.0。
- RPM 仓库增加 `exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni`，
  安装/降级时用 `--disableexcludes=kubernetes` 临时解除。
- 构建产物：`kubedeploy-1.1.0.tar.gz`。

### 验证

- 36 个自动化测试通过。
- 未在真实 RHEL 8.4 节点上重新执行完整集群安装；本次修复依据现场日志复现路径。
