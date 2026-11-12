"""CNI 部署：Calico（默认）或 Flannel。"""

from __future__ import annotations

import os
import re

from .agent_base import ActionFailure, ensure_root, log, read_file, run, run_ok, write_file
from .constants import (CONTAINERD_SOCK, DEFAULT_CALICO_IMAGE_REPOSITORY,
                        DEFAULT_CALICO_VERSION, DEFAULT_CNI,
                        DEFAULT_FLANNEL_CNI_IMAGE, DEFAULT_FLANNEL_IMAGE)

CALICO_RESOURCE_DIR = os.path.join(os.path.dirname(__file__), "resources", "calico")


MANIFEST = r'''---
apiVersion: v1
kind: Namespace
metadata:
  name: kube-flannel
  labels:
    k8s-app: flannel
    pod-security.kubernetes.io/enforce: privileged
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: flannel
  namespace: kube-flannel
  labels:
    k8s-app: flannel
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: flannel
  labels:
    k8s-app: flannel
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["nodes/status"]
    verbs: ["patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: flannel
  labels:
    k8s-app: flannel
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: flannel
subjects:
  - kind: ServiceAccount
    name: flannel
    namespace: kube-flannel
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-flannel-cfg
  namespace: kube-flannel
  labels:
    tier: node
    k8s-app: flannel
    app: flannel
data:
  cni-conf.json: |
    {
      "name": "cbr0",
      "cniVersion": "0.3.1",
      "plugins": [
        {
          "type": "flannel",
          "delegate": {
            "hairpinMode": true,
            "isDefaultGateway": true
          }
        },
        {
          "type": "portmap",
          "capabilities": {
            "portMappings": true
          }
        }
      ]
    }
  net-conf.json: |
    {
      "Network": "__POD_CIDR__",
      "EnableNFTables": false,
      "Backend": {
        "Type": "vxlan"
      }
    }
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: kube-flannel-ds
  namespace: kube-flannel
  labels:
    tier: node
    app: flannel
    k8s-app: flannel
spec:
  selector:
    matchLabels:
      app: flannel
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
  template:
    metadata:
      labels:
        tier: node
        app: flannel
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: kubernetes.io/os
                    operator: In
                    values: ["linux"]
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      priorityClassName: system-node-critical
      tolerations:
        - operator: Exists
          effect: NoSchedule
        - operator: Exists
          effect: NoExecute
      serviceAccountName: flannel
      initContainers:
        - name: install-cni-plugin
          image: __FLANNEL_CNI_IMAGE__
          command: ["cp"]
          args: ["-f", "/flannel", "/opt/cni/bin/flannel"]
          volumeMounts:
            - name: cni-plugin
              mountPath: /opt/cni/bin
        - name: install-cni
          image: __FLANNEL_IMAGE__
          command: ["cp"]
          args: ["-f", "/etc/kube-flannel/cni-conf.json", "/etc/cni/net.d/10-flannel.conflist"]
          volumeMounts:
            - name: cni
              mountPath: /etc/cni/net.d
            - name: flannel-cfg
              mountPath: /etc/kube-flannel/
      containers:
        - name: kube-flannel
          image: __FLANNEL_IMAGE__
          command: ["/opt/bin/flanneld"]
          args:
            - "--ip-masq"
            - "--kube-subnet-mgr"
            __IFACE_ARG__
          resources:
            requests:
              cpu: "100m"
              memory: "50Mi"
          securityContext:
            privileged: false
            capabilities:
              add: ["NET_ADMIN", "NET_RAW"]
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            - name: EVENT_QUEUE_DEPTH
              value: "5000"
            - name: CONT_WHEN_CACHE_NOT_READY
              value: "false"
          volumeMounts:
            - name: run
              mountPath: /run/flannel
            - name: flannel-cfg
              mountPath: /etc/kube-flannel/
            - name: xtables-lock
              mountPath: /run/xtables.lock
      volumes:
        - name: run
          hostPath:
            path: /run/flannel
        - name: cni-plugin
          hostPath:
            path: /opt/cni/bin
        - name: cni
          hostPath:
            path: /etc/cni/net.d
        - name: flannel-cfg
          configMap:
            name: kube-flannel-cfg
        - name: xtables-lock
          hostPath:
            path: /run/xtables.lock
            type: FileOrCreate
'''


def render_manifest(params):
    pod_cidr = params.get("pod_cidr") or "10.244.0.0/16"
    flannel_image = params.get("flannel_image") or DEFAULT_FLANNEL_IMAGE
    cni_image = params.get("flannel_cni_image") or DEFAULT_FLANNEL_CNI_IMAGE
    iface = (params.get("flannel_iface") or "").strip()
    iface_arg = '- "--iface=%s"' % iface if iface else ""
    text = MANIFEST.replace("__POD_CIDR__", pod_cidr)
    text = text.replace("__FLANNEL_CNI_IMAGE__", cni_image)
    text = text.replace("__FLANNEL_IMAGE__", flannel_image)
    text = text.replace("__IFACE_ARG__", iface_arg)
    return text


def _calico_manifest_path(version):
    path = os.path.join(CALICO_RESOURCE_DIR, version, "calico.yaml")
    if not os.path.isfile(path):
        raise ActionFailure("缺少 Calico %s 清单文件" % version,
                            hint="重新解压完整的 KubeDeploy 发布包")
    return path


def _ensure_documents(text):
    """DaoCloud raw 文件可能丢失 --- 分隔符；按 # Source 注释补齐。"""
    lines = text.splitlines()
    out = []
    for line in lines:
        if line.startswith("# Source:"):
            previous = next((x for x in reversed(out) if x.strip()), "")
            if previous and previous.strip() != "---":
                out.append("---")
        out.append(line)
    return "\n".join(out) + "\n"


def _calico_backend(encapsulation):
    if encapsulation == "vxlan":
        return "Never", "Always"
    if encapsulation == "none":
        return "Never", "Never"
    return "Always", "Never"


def render_calico_manifest(params):
    version = params.get("calico_version") or DEFAULT_CALICO_VERSION
    pod_cidr = params.get("pod_cidr") or "10.244.0.0/16"
    repo = (params.get("calico_image_repository")
            or DEFAULT_CALICO_IMAGE_REPOSITORY).rstrip("/")
    encapsulation = params.get("calico_encapsulation") or "ipip"
    text = read_file(_calico_manifest_path(version))
    if "calico-node" not in text or "kind: DaemonSet" not in text:
        raise ActionFailure("Calico 清单文件内容异常")
    text = _ensure_documents(text)
    # 国内镜像代理替换；Calico 官方清单使用 quay.io/calico。
    text = text.replace("quay.io/calico/", repo + "/")
    text = text.replace("docker.io/calico/", repo + "/")
    ipip, vxlan = _calico_backend(encapsulation)
    # 修改默认 IPPool 的封装方式（清单默认 IPIP=Always/VXLAN=Never）。
    for name, value in (("CALICO_IPV4POOL_IPIP", ipip),
                        ("CALICO_IPV4POOL_VXLAN", vxlan)):
        pattern = r'(?m)^(\s*- name: %s\s*\n\s*value:\s*)"[^"]*"' % name
        text, count = re.subn(pattern, lambda m: m.group(1) + '"%s"' % value, text)
        if count == 0:
            raise ActionFailure("Calico 清单缺少 %s 环境变量" % name)
    # 启用 CALICO_IPV4POOL_CIDR（标准清单中默认被注释）。
    cidr_block = ('            - name: CALICO_IPV4POOL_CIDR\n'
                  '              value: "%s"' % pod_cidr)
    pattern = (r'(?m)^\s*#\s*-\s*name:\s*CALICO_IPV4POOL_CIDR\s*\n'
               r'\s*#\s*value:\s*"[^"]*"')
    text, count = re.subn(pattern, cidr_block, text)
    if count == 0:
        if "CALICO_IPV4POOL_CIDR" in text:
            text = re.sub(r'(?m)^(\s*- name: CALICO_IPV4POOL_CIDR\s*\n\s*value:\s*)"[^"]*"',
                          lambda m: m.group(1) + '"%s"' % pod_cidr, text)
        else:
            text = text.replace('            - name: CALICO_IPV4POOL_IPIP',
                                cidr_block + '\n            - name: CALICO_IPV4POOL_IPIP', 1)
    # 节点有多网卡时使用 Kubernetes InternalIP，避免选错网卡。
    auto = ('            - name: IP_AUTODETECTION_METHOD\n'
            '              value: "kubernetes-internal-ip"')
    if "IP_AUTODETECTION_METHOD" not in text:
        text = text.replace('            - name: IP\n              value: "autodetect"',
                            '            - name: IP\n              value: "autodetect"\n' + auto, 1)
    return text


def _calico_images(text):
    images = []
    for image in re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", text):
        if image not in images:
            images.append(image)
    return images


def preload_calico(params):
    text = render_calico_manifest(params)
    images = _calico_images(text)
    pulled = []
    for image in images:
        log("预拉取 Calico 镜像 %s" % image)
        proc = run(["crictl", "--runtime-endpoint", CONTAINERD_SOCK, "pull", image],
                   timeout=900, check=False)
        if proc.returncode != 0:
            raise ActionFailure("Calico 镜像拉取失败：%s" % image,
                                errors=[proc.stderr.strip()[-900:]],
                                hint="检查 docker.m.daocloud.io 连通性，"
                                     "或用 --calico-image-repository 指定内网镜像")
        pulled.append(image)
    return {"images": pulled}


def deploy_calico(params):
    ensure_root()
    text = render_calico_manifest(params)
    kubeconfig = "/etc/kubernetes/admin.conf"
    if not os.path.exists(kubeconfig):
        raise ActionFailure("admin.conf 不存在，Calico 只能在控制平面节点部署")
    proc = run(["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
               input_data=text.encode("utf-8"), timeout=600, check=False)
    if proc.returncode != 0:
        raise ActionFailure("Calico 清单应用失败",
                            errors=[proc.stderr.strip()[-1800:]],
                            hint="检查 Calico 镜像、CRD 与 API Server 状态")
    for args, desc in (
        (["-n", "kube-system", "rollout", "status", "daemonset/calico-node",
          "--timeout=600s"], "calico-node"),
        (["-n", "kube-system", "rollout", "status",
          "deployment/calico-kube-controllers", "--timeout=300s"], "calico-kube-controllers"),
    ):
        proc = run(["kubectl", "--kubeconfig", kubeconfig] + args, timeout=660, check=False)
        if proc.returncode != 0:
            logs = run_ok(["kubectl", "--kubeconfig", kubeconfig, "-n", "kube-system",
                           "get", "pods", "-o", "wide"], timeout=60)
            raise ActionFailure("%s 未就绪" % desc,
                                errors=[(proc.stderr + "\n" + logs.stdout)[-1800:]],
                                hint="检查 Calico 镜像拉取、calico-node 日志与节点网络")
    return {"deployed": True, "cni": "calico", "pod_cidr": params.get("pod_cidr"),
            "encapsulation": params.get("calico_encapsulation") or "ipip"}


def preload_cni(params):
    if (params.get("cni") or DEFAULT_CNI) == "calico":
        return preload_calico(params)
    images = [params.get("flannel_image") or DEFAULT_FLANNEL_IMAGE,
              params.get("flannel_cni_image") or DEFAULT_FLANNEL_CNI_IMAGE]
    pulled = []
    for image in images:
        log("预拉取 CNI 镜像 %s" % image)
        proc = run(["crictl", "--runtime-endpoint", CONTAINERD_SOCK, "pull", image],
                   timeout=900, check=False)
        if proc.returncode != 0:
            raise ActionFailure("CNI 镜像拉取失败：%s" % image,
                                errors=[proc.stderr.strip()[-800:]],
                                hint="检查 ghcr.m.daocloud.io 连通性，或通过 "
                                     "--flannel-image/--flannel-cni-image 指定内网镜像")
        pulled.append(image)
    return {"images": pulled}


def deploy_cni(params):
    if (params.get("cni") or DEFAULT_CNI) == "calico":
        return deploy_calico(params)
    ensure_root()
    text = render_manifest(params)
    kubeconfig = "/etc/kubernetes/admin.conf"
    if not os.path.exists(kubeconfig):
        raise ActionFailure("admin.conf 不存在，CNI 只能在控制平面节点部署")
    proc = run(["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
               input_data=text.encode("utf-8"), timeout=300, check=False)
    if proc.returncode != 0:
        raise ActionFailure("CNI 清单应用失败", errors=[proc.stderr.strip()[-1200:]])
    proc = run(["kubectl", "--kubeconfig", kubeconfig, "-n", "kube-flannel",
                "rollout", "status", "daemonset/kube-flannel-ds", "--timeout=300s"],
               timeout=360, check=False)
    if proc.returncode != 0:
        logs = run_ok(["kubectl", "--kubeconfig", kubeconfig, "-n", "kube-flannel",
                       "get", "pods", "-o", "wide"], timeout=60)
        raise ActionFailure("Flannel DaemonSet 未就绪",
                            errors=[(proc.stderr + "\n" + logs.stdout)[-1600:]],
                            hint="检查 CNI 镜像拉取与节点网络")
    return {"deployed": True, "pod_cidr": params.get("pod_cidr")}


def cni_status(params):
    kubeconfig = "/etc/kubernetes/admin.conf"
    if (params.get("cni") or DEFAULT_CNI) == "calico":
        proc = run_ok(["kubectl", "--kubeconfig", kubeconfig, "-n", "kube-system",
                       "get", "pods", "-l", "k8s-app=calico-node", "-o", "wide"], timeout=60)
    else:
        proc = run_ok(["kubectl", "--kubeconfig", kubeconfig, "-n", "kube-flannel",
                       "get", "pods", "-o", "wide"], timeout=60)
    return {"ok": proc.returncode == 0, "output": proc.stdout + proc.stderr}
