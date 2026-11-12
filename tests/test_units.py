import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock

from types import SimpleNamespace

from kubedeploy import (agent_cni, agent_downgrade, agent_ha, agent_kube,
                        agent_packages, agent_runtime, repository)
from kubedeploy.agent_client import (_bootstrap_script_for, _python_probe_command,
                                     build_agent_package, build_id)
from kubedeploy.compat import evaluate
from kubedeploy.models import ClusterSpec, NodeSpec, SshOptions, parse_node_token
from kubedeploy.state import ClusterRecord
from kubedeploy.util import (parse_version_tuple, sanitize_node_name,
                             valid_dns_subdomain)
from kubedeploy.versions import (VersionRequest, parse_version_request,
                                 select_package_version, validate_upgrade_path)


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_version_request("v1.34.6").text, "1.34.6")
        self.assertEqual(parse_version_request("1.28").minor, 28)
        self.assertIsNone(parse_version_request("1.28").patch)
        with self.assertRaises(Exception):
            parse_version_request("1.38.0")
        with self.assertRaises(Exception):
            parse_version_request("2.1.0")

    def test_select_package(self):
        candidates = ["1.36.4-1.1", "1.36.6-1.1", "1.36.5-1.1", "1.37.0-1.1"]
        raw, ver = select_package_version(candidates, VersionRequest(1, 36))
        self.assertEqual(raw, "1.36.6-1.1")
        self.assertEqual(ver, "v1.36.6")
        raw, ver = select_package_version(candidates, VersionRequest(1, 36, 5))
        self.assertEqual(ver, "v1.36.5")

    def test_upgrade_path(self):
        self.assertEqual(validate_upgrade_path("v1.34.1", "v1.35.0"), 1)
        with self.assertRaises(Exception):
            validate_upgrade_path("v1.28.0", "v1.37.0")
        self.assertEqual(validate_upgrade_path("v1.28.0", "v1.37.0", True), 9)

    def test_all_minors_parse(self):
        for minor in range(28, 38):
            self.assertEqual(parse_version_request("1.%d" % minor).minor, minor)
            self.assertEqual(parse_version_request("v1.%d.3" % minor).patch, 3)


class ModelTests(unittest.TestCase):
    def test_multi_master_requires_vip(self):
        spec = ClusterSpec(
            name="prod", version="v1.34.6",
            masters=[NodeSpec("10.0.0.1"), NodeSpec("10.0.0.2")],
            workers=[NodeSpec("10.0.0.3", role="worker")],
            ssh=SshOptions(),
        )
        with self.assertRaises(Exception):
            spec.validate()
        spec.vip = "10.0.0.100"
        spec.validate()
        self.assertEqual(spec.endpoint, "10.0.0.100:16443")

    def test_endpoint(self):
        spec = ClusterSpec(name="one", version="v1.34.6",
                           masters=[NodeSpec("10.0.0.1")], workers=[], ssh=SshOptions())
        spec.validate()
        self.assertEqual(spec.endpoint, "10.0.0.1:6443")

    def test_duplicate_node(self):
        spec = ClusterSpec(name="prod", version="v1.34.6",
                           masters=[NodeSpec("10.0.0.1")],
                           workers=[NodeSpec("10.0.0.1", role="worker")],
                           ssh=SshOptions())
        with self.assertRaises(Exception):
            spec.validate()

    def test_public_ssh_private_node_ip(self):
        node = parse_node_token("47.100.1.10=172.16.0.10", "master")
        self.assertEqual(node.address, "47.100.1.10")
        self.assertEqual(node.k8s_ip, "172.16.0.10")
        spec = ClusterSpec(name="cloud", version="v1.34.6",
                           masters=[node], workers=[], ssh=SshOptions())
        spec.validate()
        self.assertEqual(spec.endpoint, "172.16.0.10:6443")

    def test_duplicate_private_ip(self):
        a = NodeSpec("47.100.1.10", node_ip="172.16.0.10")
        b = NodeSpec("47.100.1.11", node_ip="172.16.0.10", role="worker")
        spec = ClusterSpec(name="cloud", version="v1.34.6",
                           masters=[a], workers=[b], ssh=SshOptions())
        with self.assertRaises(Exception):
            spec.validate()


class NodeAddressTests(unittest.TestCase):
    def test_auto_private_ip(self):
        from kubedeploy.models import NodeSpec
        from kubedeploy.orchestrator import NodeRuntime
        from kubedeploy.ui import Console
        node = NodeSpec("47.100.1.10")
        rt = NodeRuntime(node, None, Console(color=False), auto_node_ip=True)
        rt._resolve_node_ip({
            "ips": [{"ip": "172.16.0.10", "interface": "eth0", "prefix": 24}],
            "default_iface": "eth0",
        })
        self.assertEqual(rt.node_ip, "172.16.0.10")
        self.assertEqual(node.node_ip, "172.16.0.10")

    def test_explicit_private_ip(self):
        from kubedeploy.models import NodeSpec
        from kubedeploy.orchestrator import NodeRuntime
        from kubedeploy.ui import Console
        node = NodeSpec("47.100.1.10", node_ip="172.16.0.10")
        rt = NodeRuntime(node, None, Console(color=False), auto_node_ip=True)
        rt._resolve_node_ip({"ips": [{"ip": "10.0.0.5"}]})
        self.assertEqual(rt.node_ip, "172.16.0.10")


class RepositoryTests(unittest.TestCase):
    def test_plan_uses_v1_minor_and_arch(self):
        facts = {"pkg_mgr": "dnf", "arch": "amd64", "arch_raw": "x86_64",
                 "os_id": "rhel", "os_version": "8.4",
                 "os_name": "Red Hat Enterprise Linux 8.4"}
        plan = repository.plan(facts, 34)
        self.assertEqual(plan[0]["source"], "aliyun")
        self.assertTrue(plan[0]["url"].endswith("/v1.34/rpm"))
        self.assertEqual(plan[1]["source"], "upstream")
        self.assertTrue(plan[1]["url"].endswith("/v1.34/rpm"))

    def test_apt_arch_and_custom_template(self):
        facts = {"pkg_mgr": "apt", "arch": "arm64", "os_id": "ubuntu",
                 "os_version": "24.04"}
        base = repository.format_base(
            "https://mirror.example.com/{os_id}/{os_version}/{arch}", 34,
            "arm64", "apt", facts)
        self.assertTrue(base.endswith("/v1.34"))
        repo = repository.plan(facts, 34, base + "/v1.34", False)[0]
        line = repository.apt_source_line(repo, "/tmp/key.gpg")
        self.assertIn("arch=arm64", line)
        self.assertIn("signed-by=/tmp/key.gpg", line)

    def test_format_base_does_not_duplicate_version(self):
        self.assertEqual(repository.format_base(
            "https://mirror.example.com/kubernetes/v1.34", 34),
            "https://mirror.example.com/kubernetes/v1.34")
        self.assertEqual(repository.format_base(
            "https://mirror.example.com/kubernetes/v1.34/rpm", 34),
            "https://mirror.example.com/kubernetes/v1.34")

    def test_rpm_repo_text(self):
        facts = {"pkg_mgr": "dnf", "arch": "amd64", "os_id": "rhel",
                 "os_version": "8.4"}
        repo = repository.plan(facts, 34, None, False)[0]
        text = repository.rpm_repo_text(repo, repo["url"] + "/repodata/repomd.xml.key")
        self.assertIn("baseurl=" + repo["url"], text)
        self.assertIn("exclude=kubelet", text)


class RepoCommandTests(unittest.TestCase):
    def test_apt_uses_var_tmp(self):
        cmd = agent_packages._apt_cmd(["update"])
        self.assertEqual(cmd[0], "apt-get")
        self.assertIn("Dir::Temp=/var/tmp", cmd)
        self.assertIn("DPkg::Lock::Timeout=600", cmd)
        self.assertEqual(agent_packages._apt_env()["TMPDIR"], "/var/tmp")

    def test_rpm_candidate_list_disables_excludes(self):
        calls = []

        def fake_run_ok(args, timeout=180):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout="kubeadm.x86_64 1.34.6-1.1 kubernetes\n",
                stderr="",
            )

        with unittest.mock.patch.object(agent_packages.shutil, "which",
                                        return_value="/usr/bin/dnf"), \
                unittest.mock.patch.object(agent_packages, "run_ok",
                                           side_effect=fake_run_ok):
            candidates = agent_packages._list_rpm_candidates("kubeadm")
        self.assertTrue(calls)
        self.assertIn("--disableexcludes=kubernetes", calls[0])
        self.assertIn("1.34.6-1.1", candidates)


class KubeadmCommandTests(unittest.TestCase):
    def test_init_has_no_unsupported_timeout(self):
        params = {"version": "v1.34.6", "node_ip": "192.168.1.10",
                  "endpoint": "192.168.1.10:6443", "pod_cidr": "10.244.0.0/16",
                  "service_cidr": "10.96.0.0/12", "dns_domain": "cluster.local"}
        cmd = agent_kube._init_command(params, "node-192-168-1-10",
                                       "registry.aliyuncs.com/google_containers")
        self.assertNotIn("--timeout", cmd)
        self.assertIn("--node-name", cmd)
        self.assertIn("node-192-168-1-10", cmd)
        self.assertIn("--upload-certs", cmd)


class NodeNameTests(unittest.TestCase):
    def test_localhost_name_is_replaced(self):
        self.assertTrue(valid_dns_subdomain("node-1.example.com"))
        self.assertEqual(sanitize_node_name("localhost.localdomain", "192.168.1.10"),
                         "node-192-168-1-10")
        self.assertEqual(sanitize_node_name("Bad_Host", "10.0.0.5"),
                         "node-10-0-0-5")
        self.assertEqual(sanitize_node_name("Worker-01", "10.0.0.5"),
                         "worker-01")


class SslContextTests(unittest.TestCase):
    def test_ssl_context(self):
        from kubedeploy.agent_base import ssl_context
        ctx = ssl_context()
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.check_hostname)


class RuntimeConfigTests(unittest.TestCase):
    def test_patch_containerd(self):
        text = """version = 3
sandbox_image = "registry.k8s.io/pause:3.8"
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runc.options]
SystemdCgroup = false
[plugins."io.containerd.cri.v1.images".registry]
config_path = ''
"""
        out, changed = agent_runtime._patch_containerd_config(
            text, sandbox_image="registry.aliyuncs.com/google_containers/pause:3.10")
        self.assertTrue(changed)
        self.assertIn('sandbox_image = "registry.aliyuncs.com/google_containers/pause:3.10"', out)
        self.assertIn("SystemdCgroup = true", out)
        self.assertIn('config_path = "/etc/containerd/certs.d"', out)

    def test_patch_containerd_creates_images_section(self):
        text = """version = 3
[plugins.'io.containerd.cri.v1.runtime']
  enable_selinux = false
"""
        out, changed = agent_runtime._patch_containerd_config(
            text, sandbox_image="registry.aliyuncs.com/google_containers/pause:3.10.1")
        self.assertTrue(changed)
        self.assertIn("[plugins.'io.containerd.cri.v1.images']", out)
        self.assertIn('sandbox_image = "registry.aliyuncs.com/google_containers/pause:3.10.1"', out)

    def test_patch_containerd_inserts_sandbox_in_section(self):
        text = """version = 3
[plugins.'io.containerd.cri.v1.images']
  image_pull_progress_timeout = '5m0s'
[plugins.'io.containerd.cri.v1.runtime']
  enable_selinux = false
"""
        out, changed = agent_runtime._patch_containerd_config(
            text, sandbox_image="registry.aliyuncs.com/google_containers/pause:3.10.1")
        self.assertTrue(changed)
        section = out.index("[plugins.'io.containerd.cri.v1.images']")
        sandbox = out.index("sandbox_image")
        runtime = out.index("[plugins.'io.containerd.cri.v1.runtime']")
        self.assertLess(section, sandbox)
        self.assertLess(sandbox, runtime)

    def test_patch_containerd_v2(self):
        text = """version = 2
[plugins."io.containerd.grpc.v1.cri"]
  sandbox_image = "registry.k8s.io/pause:3.9"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
    SystemdCgroup = false
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = ""
"""
        out, _ = agent_runtime._patch_containerd_config(text, "example.com/pause:3.9")
        self.assertIn('example.com/pause:3.9', out)
        self.assertIn("SystemdCgroup = true", out)
        self.assertIn('config_path = "/etc/containerd/certs.d"', out)


class HaTests(unittest.TestCase):
    def test_vrrp_stable(self):
        self.assertEqual(agent_ha._vrrp_id("prod", ["a", "b"]),
                         agent_ha._vrrp_id("prod", ["a", "b", "c"]))
        self.assertNotEqual(agent_ha._vrrp_id("prod"), agent_ha._vrrp_id("dev"))

    def test_keepalived(self):
        params = {"vip": "10.0.0.100", "node_ip": "10.0.0.1",
                  "masters": ["10.0.0.1", "10.0.0.2"], "port": 8443,
                  "cluster_name": "prod", "is_first": True}
        facts = {"ips": [{"interface": "eth0", "prefix": 24, "cidr": "10.0.0.1/24"}],
                 "default_iface": "eth0"}
        text = agent_ha._keepalived_config(params, facts)
        self.assertIn("virtual_router_id", text)
        self.assertIn("10.0.0.100/24 dev eth0", text)
        self.assertIn("unicast_peer", text)


class CalicoTests(unittest.TestCase):
    def test_render_calico_ipip(self):
        text = agent_cni.render_calico_manifest({
            "pod_cidr": "10.244.0.0/16",
            "calico_image_repository": "docker.m.daocloud.io/calico",
            "calico_encapsulation": "ipip",
        })
        self.assertIn("docker.m.daocloud.io/calico/node:v3.32.0", text)
        self.assertIn("docker.m.daocloud.io/calico/cni:v3.32.0", text)
        self.assertNotIn("quay.io/calico/", text)
        self.assertIn("CALICO_IPV4POOL_CIDR", text)
        self.assertIn('value: "10.244.0.0/16"', text)
        self.assertIn("IP_AUTODETECTION_METHOD", text)
        self.assertIn('value: "kubernetes-internal-ip"', text)
        self.assertGreaterEqual(text.count("\n---\n"), 5)

    def test_render_calico_vxlan(self):
        text = agent_cni.render_calico_manifest({
            "pod_cidr": "10.244.0.0/16",
            "calico_image_repository": "docker.m.daocloud.io/calico",
            "calico_encapsulation": "vxlan",
        })
        self.assertRegex(text, r'CALICO_IPV4POOL_IPIP\s*\n\s*value: "Never"')
        self.assertRegex(text, r'CALICO_IPV4POOL_VXLAN\s*\n\s*value: "Always"')


class CniTests(unittest.TestCase):
    def test_manifest(self):
        text = agent_cni.render_manifest({"pod_cidr": "10.244.0.0/16",
                                          "flannel_image": "example/flannel:v1",
                                          "flannel_cni_image": "example/cni:v1",
                                          "flannel_iface": "ens192"})
        self.assertIn("10.244.0.0/16", text)
        self.assertIn("example/flannel:v1", text)
        self.assertIn('"--iface=ens192"', text)
        self.assertNotIn("__", text)


class BackupParseTests(unittest.TestCase):
    def test_etcd_args(self):
        manifest = """apiVersion: v1
kind: Pod
spec:
  containers:
  - image: registry.k8s.io/etcd:3.5.12-0
    command:
    - etcd
    - --name=node1
    - --data-dir=/var/lib/etcd
    - --initial-advertise-peer-urls=https://10.0.0.1:2380
    - --initial-cluster=node1=https://10.0.0.1:2380
    - --initial-cluster-token=etcd-cluster
"""
        args = agent_downgrade._etcd_args(manifest)
        self.assertEqual(args["name"], "node1")
        self.assertEqual(args["peer"], "https://10.0.0.1:2380")
        self.assertEqual(args["token"], "etcd-cluster")
        self.assertIn("registry.k8s.io/etcd", args["image"])


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_script(self):
        text = _bootstrap_script_for("x86_64")
        self.assertIn("cpython-3.11.10%2B20241016-x86_64", text)
        self.assertIn("8b50a442", text)
        self.assertIn("for u in $URLS", text)
        self.assertNotIn("__URLS__", text)
        arm = _bootstrap_script_for("aarch64")
        self.assertIn("aarch64", arm)
        probe = _python_probe_command()
        self.assertIn("python3.11", probe)


class CompatibilityTests(unittest.TestCase):
    def test_cgroup_v1_blocked_for_135(self):
        facts = {"os_family": "rhel", "os_id": "centos", "os_version": "7",
                 "systemd": True, "is_root": True, "arch": "amd64",
                 "cgroup_version": "1", "kernel": "3.10.0", "existing_cluster": {},
                 "memory_mb": 4096, "disk_free_gb": 50, "cpu_count": 4,
                 "ports_busy": [], "swap_total_mb": 0}
        errors, warnings = evaluate(facts, 35, mode="install")
        self.assertTrue(any("cgroup v2" in e for e in errors))

    def test_cloud_vip_warning(self):
        facts = {"os_family": "debian", "os_id": "ubuntu", "os_version": "24.04",
                 "systemd": True, "is_root": True, "arch": "amd64",
                 "cgroup_version": "2", "kernel": "6.8.0", "existing_cluster": {},
                 "memory_mb": 4096, "disk_free_gb": 50, "cpu_count": 4,
                 "ports_busy": [], "swap_total_mb": 0, "cloud_hint": "AWS EC2"}
        errors, warnings = evaluate(facts, 34, mode="install", internal_ha=True)
        self.assertTrue(any("外部 LB" in w for w in warnings))

    def test_cgroup_v2_allows_137(self):
        facts = {"os_family": "debian", "os_id": "ubuntu", "os_version": "24.04",
                 "systemd": True, "is_root": True, "arch": "amd64",
                 "cgroup_version": "2", "kernel": "6.8.0", "existing_cluster": {},
                 "memory_mb": 4096, "disk_free_gb": 50, "cpu_count": 4,
                 "ports_busy": [], "swap_total_mb": 0}
        errors, warnings = evaluate(facts, 37, mode="install")
        self.assertFalse(errors, errors)


class PackageTests(unittest.TestCase):
    def test_agent_tar(self):
        data = build_agent_package()
        self.assertGreater(len(data), 1000)
        with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertIn("VERSION", names)
        self.assertIn("kubedeploy/agent.py", names)
        self.assertIn("kubedeploy/agent_downgrade.py", names)
        self.assertIn("kubedeploy/resources/calico/v3.32.0/calico.yaml", names)
        self.assertTrue(build_id())


class CliTests(unittest.TestCase):
    def test_help(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["KUBEDEPLOY_HOME"] = os.path.join(root, ".test-state")
        proc = subprocess.run([sys.executable, "-m", "kubedeploy.cli", "--help"],
                              cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"single", proc.stdout)

    def test_cloud_mapping_dry_run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["KUBEDEPLOY_HOME"] = os.path.join(root, ".test-state")
        proc = subprocess.run([
            sys.executable, "-m", "kubedeploy.cli", "cluster",
            "--masters", "47.100.1.11=172.16.0.11,47.100.1.12=172.16.0.12,47.100.1.13=172.16.0.13",
            "--workers", "47.100.1.21=172.16.0.21",
            "--control-plane-endpoint", "k8s-lb.example.com:6443",
            "--version", "1.34.6", "--dry-run", "--no-color",
        ], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out = proc.stdout.decode("utf-8", "replace")
        self.assertIn("172.16.0.11", out)
        self.assertIn("外部负载均衡", out)

    def test_dry_run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["KUBEDEPLOY_HOME"] = os.path.join(root, ".test-state")
        proc = subprocess.run([sys.executable, "-m", "kubedeploy.cli", "single",
                               "--node", "10.0.0.11", "--version", "1.34.6",
                               "--dry-run", "--no-color"],
                              cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"dry-run", proc.stdout)


if __name__ == "__main__":
    unittest.main()
