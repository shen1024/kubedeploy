import base64
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from kubedeploy import orchestrator
from kubedeploy.models import ClusterSpec, NodeSpec, SshOptions
from kubedeploy.state import ClusterRecord


class FakeClient:
    registry = {}
    version = "v1.34.6"

    def __init__(self, address, ssh_options=None, console=None, local=None, force_upload=False):
        self.address = address
        self.local = True
        self.console = console
        self.closed = False
        self.calls = []
        FakeClient.registry[address] = self

    def prepare(self):
        pass

    def call(self, action, params=None, timeout=1800, stream=True, check=True):
        params = params or {}
        self.calls.append((action, params))
        host = "node-" + self.address.replace(".", "-")
        facts = {
            "hostname": host, "os_id": "ubuntu", "os_name": "Ubuntu",
            "os_version": "24.04", "os_family": "debian", "pkg_mgr": "apt",
            "arch": "amd64", "kernel": "6.8.0", "cgroup_version": "2",
            "systemd": True, "is_root": True, "memory_mb": 8192,
            "disk_free_gb": 100, "cpu_count": 4, "swap_total_mb": 0,
            "existing_cluster": {"installed": False},
            "ports_busy": [], "master_ports_busy": [], "worker_ports_busy": [],
            "firewall_active": False, "selinux": "Disabled", "time_synced": True,
            "ips": [{"ip": self.address, "interface": "eth0", "prefix": 24,
                     "cidr": self.address + "/24"}],
            "endpoint_port_busy": False,
        }
        if action == "detect":
            return facts
        if action == "preflight":
            return {"facts": facts, "errors": [], "warnings": []}
        if action == "prepare":
            return {"changes": [], "warnings": []}
        if action == "install_packages":
            return {"k8s_version": "v1.34.6", "packages": {}, "fallback": False,
                    "warnings": []}
        if action == "install_runtime":
            return {"containerd_version": "2.1.9"}
        if action == "check_packages":
            return {"k8s_version": FakeClient.version, "packages": {},
                    "fallback": False}
        if action == "backup":
            return {"snapshot_path": "/var/lib/kube-deploy/backups/x/snap.db",
                    "snapshot_sha256": "a" * 64,
                    "archive_path": "/var/lib/kube-deploy/backups/x/cp.tgz",
                    "archive_sha256": "b" * 64}
        if action == "drain_node":
            return {"ok": True, "output": ""}
        if action == "uncordon_node":
            return {"node": params.get("node_name")}
        if action in ("upgrade_apply", "upgrade_control_plane_node", "upgrade_worker_node"):
            return {"target": params.get("target")}
        if action == "wait_ready":
            return {"ready": params.get("node_names", [])}
        if action == "write_state":
            return {"path": "/var/lib/kube-deploy/state.json"}
        if action == "init":
            admin = "apiVersion: v1\nclusters: []\n"
            return {"node_name": host, "endpoint": "10.0.0.100:8443",
                    "image_repository": "registry.aliyuncs.com/google_containers",
                    "pause_image": "registry.aliyuncs.com/google_containers/pause:3.10",
                    "admin_conf_b64": base64.b64encode(admin.encode()).decode(),
                    "join": {"token": "x.y", "ca_hash": "sha256:abc",
                             "certificate_key": "a" * 64}}
        if action == "generate_join":
            return {"token": "x.y", "ca_hash": "sha256:abc",
                    "certificate_key": "a" * 64}
        if action == "join":
            return {"node_name": host, "control_plane": bool(params.get("control_plane"))}
        if action == "setup_ha":
            return {"interface": "eth0", "vip": "10.0.0.100", "port": 8443}
        if action == "ha_status":
            return {"has_vip": self.address == "10.0.0.11", "haproxy": True,
                    "keepalived": True, "vip": "10.0.0.100"}
        if action == "preload_cni":
            return {"images": []}
        if action == "deploy_cni":
            return {"deployed": True}
        if action == "wait_ready":
            return {"ready": params.get("node_names", [])}
        if action == "untaint_master":
            return {"untainted": host}
        if action == "read_backup_file":
            return {"snapshot_b64": base64.b64encode(b"snapshot").decode(),
                    "sha256": "c" * 64, "size": 8,
                    "path": params.get("snapshot_path")}
        if action == "verify_backup":
            return {"ok": True, "files": []}
        if action == "write_backup_file":
            return {"path": params.get("dest_path"), "sha256": params.get("sha256")}
        if action == "kubelet_action":
            return {"active": False, "operation": params.get("operation")}
        if action == "restore_control_plane":
            return {"backup_id": params.get("backup_id"),
                    "node": params.get("node_name"), "restored_at": "now"}
        if action == "start_node":
            return {"started": True}
        if action == "cluster_info":
            nodes = []
            for addr in FakeClient.registry:
                nodes.append({"name": "node-" + addr.replace(".", "-"),
                              "role": "worker", "ip": addr,
                              "version": FakeClient.version, "ready": True})
            return {"kubeconfig_exists": True, "version": FakeClient.version,
                    "nodes": nodes, "endpoint": "10.0.0.100:8443",
                    "admin_conf_b64": base64.b64encode(b"apiVersion: v1\n").decode()}
        if action == "write_state":
            return {"path": "/var/lib/kube-deploy/state.json"}
        return {}

    def close(self):
        self.closed = True


class OrchestratorMockTests(unittest.TestCase):
    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tmp = os.path.join(root, "test-state-mock")
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.old_home = os.environ.get("KUBEDEPLOY_HOME")
        os.environ["KUBEDEPLOY_HOME"] = self.tmp
        self.old_client = orchestrator.NodeAgentClient
        self.old_local = orchestrator.is_local_address
        self.old_save = ClusterRecord.save
        self.old_save_kubeconfig = ClusterRecord.save_kubeconfig
        self.old_default_kubeconfig = orchestrator.install_default_kubeconfig
        ClusterRecord.save = lambda rec: os.path.join(self.tmp, "record.json")
        ClusterRecord.save_kubeconfig = lambda rec, content: os.path.join(self.tmp, "kubeconfig.conf")
        orchestrator.install_default_kubeconfig = lambda content: (
            os.path.join(self.tmp, "default-kubeconfig.conf"), True)
        orchestrator.NodeAgentClient = FakeClient
        # 模拟“控制端不是集群节点”，确保走 FakeClient 远程路径。
        orchestrator.is_local_address = lambda addr: False
        FakeClient.registry = {}
        FakeClient.version = "v1.34.6"

    def tearDown(self):
        orchestrator.NodeAgentClient = self.old_client
        orchestrator.is_local_address = self.old_local
        ClusterRecord.save = self.old_save
        ClusterRecord.save_kubeconfig = self.old_save_kubeconfig
        orchestrator.install_default_kubeconfig = self.old_default_kubeconfig
        if self.old_home is None:
            os.environ.pop("KUBEDEPLOY_HOME", None)
        else:
            os.environ["KUBEDEPLOY_HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _options(self):
        return SimpleNamespace(dry_run=False, preflight_only=False, force_remote=False,
                               force_upload=False, allow_untested_os=False,
                               containerd_version=None, runc_version=None,
                               containerd_sha256=None, runc_sha256=None, yes=True)

    def _console(self):
        from kubedeploy.ui import Console
        return Console(color=False)

    def test_single_install(self):
        spec = ClusterSpec(name="test", version="v1.34.6",
                           masters=[NodeSpec("10.0.0.11")], workers=[],
                           ssh=SshOptions())
        d = orchestrator.Deployer(spec, self._console(), self._options())
        rec = d.install()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.version, "v1.34.6")
        self.assertEqual(rec.node_count, 1)
        actions = [a for a, _ in FakeClient.registry["10.0.0.11"].calls]
        for action in ("init", "deploy_cni", "untaint_master", "write_state"):
            self.assertIn(action, actions)

    def test_multi_install(self):
        spec = ClusterSpec(
            name="test-ha", version="v1.34.6",
            masters=[NodeSpec("10.0.0.11"), NodeSpec("10.0.0.12")],
            workers=[NodeSpec("10.0.0.21", role="worker")],
            vip="10.0.0.100", ssh=SshOptions())
        d = orchestrator.Deployer(spec, self._console(), self._options())
        rec = d.install()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.master_count, 2)
        first = [a for a, _ in FakeClient.registry["10.0.0.11"].calls]
        second = [a for a, _ in FakeClient.registry["10.0.0.12"].calls]
        self.assertIn("setup_ha", first)
        self.assertIn("join", second)
        self.assertIn("setup_ha", second)


class ExistingClusterCloudMappingTests(unittest.TestCase):
    def test_build_spec_preserves_ssh_address(self):
        from kubedeploy.lifecycle import ExistingCluster
        from kubedeploy.ui import Console
        spec = ClusterSpec(
            name="cloud", version="v1.34.6",
            masters=[NodeSpec("47.100.1.11", role="master",
                              hostname="node-11", node_ip="172.16.0.11")],
            workers=[], vip=None, ssh=SshOptions(),
        )
        ec = ExistingCluster.__new__(ExistingCluster)
        ec.console = Console(color=False)
        ec.ssh = SshOptions()
        ec.options = SimpleNamespace(allow_untested_os=False, skip_backup=False,
                                     no_upstream_fallback=False)
        ec.current_version = "v1.34.6"
        ec.remote_state = {"spec": spec.to_dict()}
        ec.nodes_info = [{"name": "node-11", "role": "master",
                          "ip": "172.16.0.11"}]
        ec.info = {"endpoint": "172.16.0.11:6443",
                   "pod_cidr": "10.244.0.0/16",
                   "service_cidr": "10.96.0.0/12"}
        built = ec._build_spec()
        self.assertEqual(built.masters[0].address, "47.100.1.11")
        self.assertEqual(built.masters[0].k8s_ip, "172.16.0.11")
        self.assertEqual(built.endpoint, "172.16.0.11:6443")


class LifecycleUpgradeMockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kubedeploy-lifecycle-")
        self.old_client = orchestrator.NodeAgentClient
        self.old_save = ClusterRecord.save
        self.old_save_kubeconfig = ClusterRecord.save_kubeconfig
        orchestrator.NodeAgentClient = FakeClient
        ClusterRecord.save = lambda rec: os.path.join(self.tmp, "record.json")
        ClusterRecord.save_kubeconfig = lambda rec, content: os.path.join(self.tmp, "kubeconfig.conf")
        FakeClient.registry = {}
        FakeClient.version = "v1.35.1"

    def tearDown(self):
        orchestrator.NodeAgentClient = self.old_client
        ClusterRecord.save = self.old_save
        ClusterRecord.save_kubeconfig = self.old_save_kubeconfig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upgrade_sequence(self):
        from kubedeploy.ui import Console
        from kubedeploy.lifecycle import ExistingCluster
        spec = ClusterSpec(
            name="up", version="v1.34.6",
            masters=[NodeSpec("10.0.0.11"), NodeSpec("10.0.0.12")],
            workers=[NodeSpec("10.0.0.21", role="worker")],
            vip="10.0.0.100", ssh=SshOptions())
        rec = ClusterRecord(name="up", version="v1.34.6",
                            endpoint="10.0.0.100:8443", spec=spec.to_dict(),
                            node_count=3, master_count=2, worker_count=1)
        ec = ExistingCluster.__new__(ExistingCluster)
        ec.bootstrap = "10.0.0.11"
        ec.ssh = SshOptions()
        ec.console = Console(color=False)
        ec.options = SimpleNamespace(allow_untested_os=False, skip_backup=False,
                                     no_upstream_fallback=False)
        ec.current_version = "v1.34.6"
        ec.spec = spec
        ec.record = rec
        ec.bootstrap_client = FakeClient("10.0.0.11")
        ec.runtimes = {}
        for node in spec.all_nodes:
            rt = orchestrator.NodeRuntime(node, FakeClient(node.address), ec.console)
            rt.facts = {"hostname": "node-" + node.address.replace(".", "-")}
            rt.hostname = "node-" + node.address.replace(".", "-")
            rt.arch = "amd64"
            ec.runtimes[node.address] = rt
        ec.build_runtimes = lambda only_addresses=None: ec.runtimes
        ec.save_state = lambda: None
        ec.refresh_kubeconfig = lambda: None
        ec.upgrade("1.35.1")
        self.assertEqual(ec.current_version, "v1.35.1")
        self.assertEqual(len(rec.backups), 1)
        actions = [a for a, _ in ec.runtimes["10.0.0.11"].client.calls]
        for action in ("backup", "drain_node", "upgrade_apply", "uncordon_node"):
            self.assertIn(action, actions)

    def test_downgrade_restore_sequence(self):
        from kubedeploy.ui import Console
        from kubedeploy.lifecycle import ExistingCluster
        spec = ClusterSpec(
            name="down", version="v1.34.6",
            masters=[NodeSpec("10.0.0.11"), NodeSpec("10.0.0.12")],
            workers=[NodeSpec("10.0.0.21", role="worker")],
            vip="10.0.0.100", ssh=SshOptions())
        backup_nodes = []
        for addr in ("10.0.0.11", "10.0.0.12"):
            backup_nodes.append({
                "address": addr, "node_name": "node-" + addr.replace(".", "-"),
                "snapshot_path": "/var/lib/kube-deploy/backups/b1/%s/snap.db" % addr,
                "snapshot_sha256": "c" * 64,
                "archive_path": "/var/lib/kube-deploy/backups/b1/%s/cp.tgz" % addr,
                "archive_sha256": "d" * 64,
            })
        rec = ClusterRecord(name="down", version="v1.35.1",
                            endpoint="10.0.0.100:8443", spec=spec.to_dict(),
                            node_count=3, master_count=2, worker_count=1)
        rec.backups = [{"backup_id": "b1", "from_version": "v1.34.6",
                        "to_version": "v1.35.1", "created_at": "2026-01-01",
                        "nodes": backup_nodes}]
        ec = ExistingCluster.__new__(ExistingCluster)
        ec.bootstrap = "10.0.0.11"
        ec.ssh = SshOptions()
        ec.console = Console(color=False)
        ec.options = SimpleNamespace(allow_untested_os=False, skip_backup=False,
                                     no_upstream_fallback=False)
        ec.current_version = "v1.35.1"
        ec.spec = spec
        ec.spec.version = "v1.35.1"
        ec.record = rec
        ec.bootstrap_client = FakeClient("10.0.0.11")
        ec.runtimes = {}
        for node in spec.all_nodes:
            rt = orchestrator.NodeRuntime(node, FakeClient(node.address), ec.console)
            rt.facts = {"hostname": "node-" + node.address.replace(".", "-")}
            rt.hostname = "node-" + node.address.replace(".", "-")
            rt.arch = "amd64"
            ec.runtimes[node.address] = rt
        ec.build_runtimes = lambda only_addresses=None: ec.runtimes
        ec.save_state = lambda: None
        ec.refresh_kubeconfig = lambda: None
        FakeClient.version = "v1.34.6"
        ec.downgrade("1.34.6", confirm=True)
        self.assertEqual(ec.current_version, "v1.34.6")
        actions = [a for a, _ in ec.runtimes["10.0.0.11"].client.calls]
        for action in ("read_backup_file", "verify_backup", "restore_control_plane"):
            self.assertIn(action, actions)


if __name__ == "__main__":
    unittest.main()
