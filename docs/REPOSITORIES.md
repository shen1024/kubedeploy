# Kubernetes 软件源自动配置与排错

## 1. 目标

用户只需要运行：

~~~bash
./k8s-deploy single --node 192.168.1.10 --version 1.34.6
~~~

工具自动完成：

1. 读取目标系统：`/etc/os-release`、包管理器（apt/dnf/yum）、CPU 架构；
2. 把 Kubernetes 版本转成仓库使用的 `v1.34` 形式；
3. 生成国内阿里云 `kubernetes-new` 仓库路径；
4. 写入 yum/dnf 或 apt 配置文件；
5. 导入 GPG key；
6. 探测仓库元数据；
7. 国内源不可用时按策略回退官方源；
8. 用正确的包版本安装 kubelet/kubeadm/kubectl，并校验实际版本。

## 2. 路径规则

| 系统 | 仓库类型 | 示例 |
|---|---|---|
| RHEL/CentOS/Rocky/Alma/openEuler | RPM | `https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm` |
| Ubuntu/Debian | DEB | `https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/deb` |
| 官方回退 | RPM | `https://pkgs.k8s.io/core:/stable:/v1.34/rpm` |
| 官方回退 | DEB | `https://pkgs.k8s.io/core:/stable:/v1.34/deb` |

> 仓库路径是 K8s `v1.34`，不是 `v34`。1.1.0 已修复此问题。

## 3. 自动识别内容

- **OS family**：debian 系使用 apt；rhel 系使用 dnf/yum；
- **OS 版本**：写入日志，并用于企业仓库模板 `{os_version}`、`{os_major}`；
- **CPU 架构**：x86_64/amd64 → `amd64`；aarch64/arm64 → `arm64`；
- **APT**：自动写 `arch=amd64` 或 `arch=arm64`；
- **RPM**：由 dnf/yum 按 `$basearch` 选择实际包；
- **K8s minor**：1.28–1.37；仓库目录使用 `v1.28`–`v1.37`。

## 4. 生成的配置

### RHEL/CentOS

文件：`/etc/yum.repos.d/kubernetes.repo`

~~~ini
[kubernetes]
name=Kubernetes 1.34 (aliyun)
baseurl=https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm/repodata/repomd.xml.key
sslverify=1
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
~~~

安装时使用 `--disableexcludes=kubernetes`，避免 exclude 阻止本次安装。

### Ubuntu/Debian

文件：`/etc/apt/sources.list.d/kube-deploy-kubernetes.list`

~~~text
deb [arch=amd64 signed-by=/usr/share/keyrings/kube-deploy-kubernetes.gpg] https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/deb /
~~~

## 5. 回退策略

默认顺序：

1. 用户 `--package-repo-base`（如果提供）；
2. 阿里云 `kubernetes-new`；
3. 官方 `pkgs.k8s.io`。

`--no-upstream-fallback` 会禁止第 3 步，适合严格国内合规环境。企业内网镜像示例：

~~~bash
./k8s-deploy single --node 10.0.0.11 --version 1.34.6 \
  --package-repo-base 'https://mirror.example.com/k8s/{os_id}/{os_version}/{arch}'
~~~

可用占位符：

- `{minor}`：`1.34`
- `{minor_num}`：`34`
- `{arch}`：`amd64` / `arm64`
- `{pkg_mgr}`：`apt` / `dnf` / `yum`
- `{os_id}`：`rhel` / `ubuntu` / `centos` ...
- `{os_version}`：`8.4` / `24.04`
- `{os_major}`：`8` / `24`
- `{arch_raw}`：`x86_64` / `aarch64`

如果模板没有包含 `v1.34`，工具会自动追加；不要手工写成 `v34`。

## 6. RHEL 8.4 证书问题

早期失败日志：

~~~text
CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
~~~

原因：便携 Python 自带的 OpenSSL 默认 CA 路径不一定包含 RHEL 的
`/etc/pki/tls/certs/ca-bundle.crt`。

1.1.0 的处理：

- 显式加载发行版 CA：
  `/etc/pki/tls/certs/ca-bundle.crt`、
  `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`、
  `/etc/ssl/certs/ca-certificates.crt` 等；
- Python urllib 失败时自动使用系统 `curl`（系统 CA）重试；
- 运行时二进制下载同样有 curl 回退。

如果仍然失败，在节点上检查：

~~~bash
curl -I https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm/repodata/repomd.xml
rpm -q ca-certificates
update-ca-trust extract
~~~

## 7. 排错命令

在目标节点手动验证：

~~~bash
# RPM
curl -fsSL https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/rpm/repodata/repomd.xml | head
dnf clean all
dnf makecache
dnf --showduplicates list kubeadm --available

# DEB
curl -fsSL https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.34/deb/InRelease | head
apt-get update
apt-cache madison kubeadm
~~~

常见错误：

| 错误 | 处理 |
|---|---|
| 404 / repomd 不存在 | 目标 minor 未同步；等待、换企业源或允许官方回退 |
| certificate verify failed | 检查系统 CA；工具会走 curl；必要时 `update-ca-trust extract` |
| GPG check FAILED | 检查 `repomd.xml.key` 可达；企业源应提供受信 key |
| 找不到目标 patch | 用 `--version 1.34` 取该 minor 最新 patch，或指定已同步 patch |
| `No matching Packages to list` | 1.1.1 起工具查询候选包时会加 `--disableexcludes=kubernetes`；旧版请升级 |
| 1.37 国内源缺失 | 允许官方回退，或配置已同步的企业源 |

## 8. 安全说明

- 默认 `gpgcheck=1`，不默认关闭签名校验；
- 仓库文件写入前会备份为 `*.kubedeploy-*.bak`；
- 不修改系统原有 OS 软件源；
- 不会自动执行 `yum update` 升级整个系统；
- 只安装 Kubernetes 组件及其依赖。
