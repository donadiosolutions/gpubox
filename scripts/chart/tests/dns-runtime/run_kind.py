#!/usr/bin/env python3
"""Run the managed-DNS chart lifecycle tests in an isolated Kind cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any


APP_REPOSITORY = "ghcr.io/donadiosolutions/gpubox"
APP_TAG = "v2.6.1"
APP_DIGEST = "sha256:b7439261c35baef39e50f2a6767a990495826b16d8151edb6295878037ac6832"
CORE_REPOSITORY = "registry.k8s.io/coredns/coredns"
CORE_TAG = "v1.14.7"
CORE_DIGEST = "sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286"
BUSYBOX_REPOSITORY = "busybox"
BUSYBOX_TAG = "1.38.0"
BUSYBOX_DIGEST = "sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616"
HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[3]
CHART = REPO / "charts/gpubox"


def command(*args: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, **kwargs)


class Suite:
    def __init__(self, kubeconfig: pathlib.Path, cluster: str, namespace_prefix: str, keep: bool) -> None:
        self.kubeconfig = kubeconfig
        self.cluster = cluster
        self.namespace_prefix = namespace_prefix
        self.keep = keep
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="gpubox-dns-kind-"))
        self.namespaces: list[str] = []

    def kubectl(self, *args: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return command("kubectl", "--kubeconfig", str(self.kubeconfig), *args, check=check, **kwargs)

    def helm(self, *args: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return command("helm", "--kubeconfig", str(self.kubeconfig), *args, check=check, **kwargs)

    def namespace(self, suffix: str) -> str:
        name = f"{self.namespace_prefix}-{suffix}"
        self.kubectl("create", "namespace", name, stdout=subprocess.DEVNULL)
        self.namespaces.append(name)
        return name

    def pod(self, namespace: str, release: str) -> dict[str, Any]:
        result = self.kubectl(
            "-n",
            namespace,
            "get",
            "pod",
            "-l",
            f"app.kubernetes.io/instance={release}",
            "-o",
            "json",
            stdout=subprocess.PIPE,
        )
        items = json.loads(result.stdout)["items"]
        assert len(items) == 1, items
        return items[0]

    def statefulset(self, namespace: str, release: str) -> dict[str, Any]:
        result = self.kubectl(
            "-n",
            namespace,
            "get",
            "statefulset",
            "-l",
            f"app.kubernetes.io/instance={release}",
            "-o",
            "json",
            stdout=subprocess.PIPE,
        )
        items = json.loads(result.stdout)["items"]
        assert len(items) == 1, items
        return items[0]

    def wait_ready(self, namespace: str, release: str, timeout: str = "15m") -> dict[str, Any]:
        self.kubectl(
            "-n",
            namespace,
            "wait",
            "pod",
            "-l",
            f"app.kubernetes.io/instance={release}",
            "--for=condition=Ready",
            f"--timeout={timeout}",
            stdout=subprocess.DEVNULL,
        )
        return self.pod(namespace, release)

    def wait_restart(self, namespace: str, release: str, container: str, before: int) -> dict[str, Any]:
        deadline = time.monotonic() + 90
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.pod(namespace, release)
            statuses = last["status"].get("initContainerStatuses", []) + last["status"].get("containerStatuses", [])
            status = next((item for item in statuses if item["name"] == container), None)
            if status and status["restartCount"] > before and status.get("ready"):
                return last
            time.sleep(1)
        raise AssertionError(f"{container} did not restart: {json.dumps(last, indent=2)}")

    def exec(self, namespace: str, pod: str, container: str, *args: str, check: bool = True) -> str:
        result = self.kubectl(
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            *args,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def values(self) -> pathlib.Path:
        path = self.tmp / "values.yaml"
        path.write_text(
            f"""image:
  repository: {APP_REPOSITORY}
  tag: {APP_TAG}
  digest: {APP_DIGEST}
  pullPolicy: IfNotPresent
command: [/bin/sleep]
args: [infinity]
containerPorts: []
resources: null
sharedMemory:
  enabled: false
service:
  enabled: false
persistence:
  home:
    enabled: false
  transfer:
    enabled: false
  tmp:
    enabled: false
  hostRoot:
    enabled: false
tailscale:
  enabled: false
  acceptDNS: false
dns:
  enabled: true
"""
        )
        return path

    def install(self, namespace: str, release: str, chart: pathlib.Path) -> None:
        self.helm(
            "upgrade",
            "--install",
            release,
            str(chart),
            "--namespace",
            namespace,
            "--values",
            str(self.values()),
            "--timeout",
            "5m",
            stdout=subprocess.DEVNULL,
        )

    def load_small_images(self) -> None:
        for image, tagged, node_source, node_digest in (
            (
                f"{CORE_REPOSITORY}:{CORE_TAG}@{CORE_DIGEST}",
                f"{CORE_REPOSITORY}:{CORE_TAG}",
                f"{CORE_REPOSITORY}:{CORE_TAG}",
                f"{CORE_REPOSITORY}@{CORE_DIGEST}",
            ),
            (
                f"{BUSYBOX_REPOSITORY}:{BUSYBOX_TAG}@{BUSYBOX_DIGEST}",
                f"{BUSYBOX_REPOSITORY}:{BUSYBOX_TAG}",
                f"docker.io/library/{BUSYBOX_REPOSITORY}:{BUSYBOX_TAG}",
                f"docker.io/library/{BUSYBOX_REPOSITORY}@{BUSYBOX_DIGEST}",
            ),
        ):
            command("docker", "tag", image, tagged)
            save = subprocess.Popen(
                ["docker", "save", tagged],
                stdout=subprocess.PIPE,
            )
            assert save.stdout is not None
            imported = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    f"{self.cluster}-control-plane",
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    "-",
                ],
                stdin=save.stdout,
                stdout=subprocess.DEVNULL,
            )
            save.stdout.close()
            save_status = save.wait()
            assert imported.returncode == 0 and save_status == 0, (image, imported.returncode, save_status)
            command(
                "docker",
                "exec",
                f"{self.cluster}-control-plane",
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "tag",
                "--force",
                node_source,
                node_digest,
                stdout=subprocess.DEVNULL,
            )

    def assert_managed_pod(self, namespace: str, release: str) -> dict[str, Any]:
        pod = self.wait_ready(namespace, release)
        name = pod["metadata"]["name"]
        init_names = [item["name"] for item in pod["spec"].get("initContainers", [])]
        assert init_names[:3] == ["dns-setup", "dns-controller", "dns"], init_names
        assert [item["name"] for item in pod["spec"]["containers"]] == ["gpubox"]
        assert self.exec(namespace, name, "dns-controller", "/usr/bin/id", "-u") == "65532"
        assert self.exec(namespace, name, "dns-controller", "/usr/bin/id", "-g") == "65532"
        write_attempt = self.kubectl(
            "-n",
            namespace,
            "exec",
            name,
            "-c",
            "gpubox",
            "--",
            "/bin/sh",
            "-c",
            "printf tamper >>/etc/resolv.conf",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert write_attempt.returncode != 0
        resolver = self.exec(namespace, name, "gpubox", "/usr/bin/cat", "/etc/resolv.conf")
        nameservers = [line for line in resolver.splitlines() if line.startswith("nameserver ")]
        assert nameservers == ["nameserver 127.0.0.1"], resolver
        assert self.exec(
            namespace,
            name,
            "gpubox",
            "/usr/bin/getent",
            "hosts",
            "kubernetes.default.svc.cluster.local",
        )
        corefile = self.exec(namespace, name, "dns-controller", "/usr/bin/cat", "/var/run/gpubox-dns/Corefile")
        assert "forward ." in corefile
        assert "100.100.100.100" not in corefile
        return pod

    @staticmethod
    def restart_count(pod: dict[str, Any], container: str) -> int:
        statuses = pod["status"].get("initContainerStatuses", []) + pod["status"].get("containerStatuses", [])
        return next(item["restartCount"] for item in statuses if item["name"] == container)

    def test_current_chart(self) -> None:
        namespace = self.namespace("current")
        release = "runtime"
        self.install(namespace, release, CHART)
        pod = self.assert_managed_pod(namespace, release)
        name = pod["metadata"]["name"]
        app_restart = self.restart_count(pod, "gpubox")
        inode = self.exec(namespace, name, "gpubox", "/usr/bin/stat", "-Lc", "%d:%i", "/etc/resolv.conf")

        controller_restart = self.restart_count(pod, "dns-controller")
        controller_status = next(
            item for item in pod["status"]["initContainerStatuses"] if item["name"] == "dns-controller"
        )
        controller_id = controller_status["containerID"].removeprefix("containerd://")
        command(
            "docker",
            "exec",
            f"{self.cluster}-control-plane",
            "crictl",
            "stop",
            controller_id,
            stdout=subprocess.DEVNULL,
        )
        pod = self.wait_restart(namespace, release, "dns-controller", controller_restart)
        assert self.restart_count(pod, "gpubox") == app_restart
        assert self.exec(namespace, name, "gpubox", "/usr/bin/stat", "-Lc", "%d:%i", "/etc/resolv.conf") == inode
        assert self.exec(namespace, name, "gpubox", "/usr/bin/getent", "hosts", "kubernetes.default.svc.cluster.local")

        core_restart = self.restart_count(pod, "dns")
        core_status = next(item for item in pod["status"]["initContainerStatuses"] if item["name"] == "dns")
        container_id = core_status["containerID"].removeprefix("containerd://")
        command("docker", "exec", f"{self.cluster}-control-plane", "crictl", "stop", container_id, stdout=subprocess.DEVNULL)
        pod = self.wait_restart(namespace, release, "dns", core_restart)
        assert self.restart_count(pod, "gpubox") == app_restart
        assert self.exec(namespace, name, "gpubox", "/usr/bin/getent", "hosts", "kubernetes.default.svc.cluster.local")

    def chart_variant(self, name: str, marker: str) -> tuple[pathlib.Path, str]:
        chart = self.tmp / name
        shutil.copytree(CHART, chart)
        controller = chart / "files/dns-controller.py"
        controller.write_bytes(controller.read_bytes() + f"\n# {marker}\n".encode())
        return chart, hashlib.sha256(controller.read_bytes().rstrip(b"\n")).hexdigest()

    def archived_chart(self) -> pathlib.Path:
        archive = self.tmp / "v2.8.2.tar"
        with archive.open("wb") as output:
            command("git", "archive", "--format=tar", "v2.8.2", "charts/gpubox", cwd=REPO, stdout=output)
        destination = self.tmp / "v2.8.2"
        destination.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
        return destination / "charts/gpubox"

    def controller_hash(self, namespace: str, pod: str) -> str:
        output = self.exec(
            namespace,
            pod,
            "dns-controller",
            "/usr/bin/sha256sum",
            "/opt/gpubox-dns/dns-controller.py",
        )
        return output.split()[0]

    def wait_projected_controller_hash(self, pod: dict[str, Any], expected: str) -> None:
        pod_uid = pod["metadata"]["uid"]
        path = (
            f"/var/lib/kubelet/pods/{pod_uid}/volumes/kubernetes.io~configmap/"
            "gpubox-dns-controller-source/dns-controller.py"
        )
        deadline = time.monotonic() + 120
        observed = ""
        while time.monotonic() < deadline:
            result = command(
                "docker",
                "exec",
                f"{self.cluster}-control-plane",
                "sha256sum",
                path,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0:
                observed = result.stdout.split()[0]
                if observed == expected:
                    return
            time.sleep(1)
        raise AssertionError(f"projected controller did not converge to {expected}; observed {observed}")

    @staticmethod
    def controller_probes(pod: dict[str, Any]) -> dict[str, Any]:
        controller = next(item for item in pod["spec"]["initContainers"] if item["name"] == "dns-controller")
        return {
            "startupProbe": controller["startupProbe"],
            "livenessProbe": controller["livenessProbe"],
        }

    def assert_controller_probes(self, namespace: str, pod: dict[str, Any]) -> None:
        name = pod["metadata"]["name"]
        for probe in self.controller_probes(pod).values():
            command_line = probe["exec"]["command"]
            self.exec(namespace, name, "dns-controller", *command_line)

    def restart_controller(self, namespace: str, release: str, pod: dict[str, Any]) -> dict[str, Any]:
        before = self.restart_count(pod, "dns-controller")
        status = next(
            item for item in pod["status"]["initContainerStatuses"] if item["name"] == "dns-controller"
        )
        container_id = status["containerID"].removeprefix("containerd://")
        command(
            "docker",
            "exec",
            f"{self.cluster}-control-plane",
            "crictl",
            "stop",
            container_id,
            stdout=subprocess.DEVNULL,
        )
        return self.wait_restart(namespace, release, "dns-controller", before)

    def test_legacy_ondelete_upgrade_rollback(self) -> None:
        namespace = self.namespace("legacy-upgrade")
        release = "legacy-upgrade"
        old_chart = self.archived_chart()
        self.install(namespace, release, old_chart)
        old_pod = self.wait_ready(namespace, release)
        old_uid = old_pod["metadata"]["uid"]
        assert "dns-controller" not in [item["name"] for item in old_pod["spec"].get("initContainers", [])]

        self.helm(
            "upgrade",
            release,
            str(CHART),
            "--namespace",
            namespace,
            "--values",
            str(self.values()),
            stdout=subprocess.DEVNULL,
        )
        assert self.pod(namespace, release)["metadata"]["uid"] == old_uid
        self.kubectl("-n", namespace, "delete", "pod", old_pod["metadata"]["name"], "--wait=true", stdout=subprocess.DEVNULL)
        upgraded = self.assert_managed_pod(namespace, release)
        upgraded_uid = upgraded["metadata"]["uid"]
        assert upgraded_uid != old_uid

        self.helm("rollback", release, "1", "--namespace", namespace, stdout=subprocess.DEVNULL)
        assert self.pod(namespace, release)["metadata"]["uid"] == upgraded_uid
        self.kubectl("-n", namespace, "delete", "pod", upgraded["metadata"]["name"], "--wait=true", stdout=subprocess.DEVNULL)
        rolled_back = self.wait_ready(namespace, release)
        assert rolled_back["metadata"]["uid"] != upgraded_uid
        assert "dns-controller" not in [item["name"] for item in rolled_back["spec"].get("initContainers", [])]

    def test_enabled_ondelete_code_freeze(self) -> None:
        namespace = self.namespace("upgrade")
        release = "upgrade"
        old_chart, old_hash = self.chart_variant("old-chart", "runtime-test old controller revision")
        new_hash = hashlib.sha256((CHART / "files/dns-controller.py").read_bytes().rstrip(b"\n")).hexdigest()
        assert old_hash != new_hash
        self.install(namespace, release, old_chart)
        old_pod = self.assert_managed_pod(namespace, release)
        old_uid = old_pod["metadata"]["uid"]
        old_probes = self.controller_probes(old_pod)
        assert self.controller_hash(namespace, old_pod["metadata"]["name"]) == old_hash
        self.assert_controller_probes(namespace, old_pod)

        self.helm(
            "upgrade",
            release,
            str(CHART),
            "--namespace",
            namespace,
            "--values",
            str(self.values()),
            stdout=subprocess.DEVNULL,
        )
        retained = self.pod(namespace, release)
        assert retained["metadata"]["uid"] == old_uid
        self.wait_projected_controller_hash(retained, new_hash)
        assert self.controller_probes(retained) == old_probes
        assert self.controller_hash(namespace, retained["metadata"]["name"]) == old_hash
        retained = self.restart_controller(namespace, release, retained)
        assert retained["metadata"]["uid"] == old_uid
        assert self.controller_probes(retained) == old_probes
        assert self.controller_hash(namespace, retained["metadata"]["name"]) == old_hash
        self.assert_controller_probes(namespace, retained)

        self.kubectl("-n", namespace, "delete", "pod", old_pod["metadata"]["name"], "--wait=true", stdout=subprocess.DEVNULL)
        upgraded = self.assert_managed_pod(namespace, release)
        upgraded_uid = upgraded["metadata"]["uid"]
        assert upgraded_uid != old_uid
        assert self.controller_hash(namespace, upgraded["metadata"]["name"]) == new_hash
        self.assert_controller_probes(namespace, upgraded)

        self.helm("rollback", release, "1", "--namespace", namespace, stdout=subprocess.DEVNULL)
        retained = self.pod(namespace, release)
        assert retained["metadata"]["uid"] == upgraded_uid
        self.wait_projected_controller_hash(retained, old_hash)
        assert self.controller_hash(namespace, retained["metadata"]["name"]) == new_hash
        retained = self.restart_controller(namespace, release, retained)
        assert retained["metadata"]["uid"] == upgraded_uid
        assert self.controller_hash(namespace, retained["metadata"]["name"]) == new_hash
        self.assert_controller_probes(namespace, retained)

        self.kubectl("-n", namespace, "delete", "pod", upgraded["metadata"]["name"], "--wait=true", stdout=subprocess.DEVNULL)
        rolled_back = self.assert_managed_pod(namespace, release)
        assert rolled_back["metadata"]["uid"] != upgraded_uid
        assert self.controller_hash(namespace, rolled_back["metadata"]["name"]) == old_hash
        assert self.controller_probes(rolled_back) == old_probes
        self.assert_controller_probes(namespace, rolled_back)

    def wait_corefile(self, namespace: str, release: str, contains_magic: bool) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        last = ""
        while time.monotonic() < deadline:
            pod = self.pod(namespace, release)
            name = pod["metadata"]["name"]
            last = self.exec(
                namespace,
                name,
                "dns-controller",
                "/usr/bin/cat",
                "/var/run/gpubox-dns/Corefile",
                check=False,
            )
            if ("100.100.100.100" in last) == contains_magic:
                return pod
            time.sleep(0.5)
        raise AssertionError(f"Corefile transition did not converge: {last}")

    def wait_dns_answer(self, namespace: str, pod: str, name: str, expected: str) -> None:
        deadline = time.monotonic() + 12
        answer = ""
        while time.monotonic() < deadline:
            answer = self.exec(
                namespace,
                pod,
                "gpubox",
                "/usr/bin/dig",
                "@127.0.0.1",
                name,
                "A",
                "+tries=1",
                "+time=2",
                "+short",
                check=False,
            )
            if answer == expected:
                return
            time.sleep(0.2)
        raise AssertionError(f"DNS did not converge for {name}: expected {expected!r}, got {answer!r}")

    def test_controlled_localapi(self) -> None:
        namespace = self.namespace("localapi")
        release = "localapi"
        values = self.tmp / "localapi-values.yaml"
        values.write_text(
            self.values().read_text().replace(
                "tailscale:\n  enabled: false\n  acceptDNS: false\n",
                """tailscale:
  enabled: true
  acceptDNS: true
  acceptRoutes: false
  authKey:
    value: test-only-never-used
  state:
    storageClass: standard
nodeSelector:
  gpubox.test/scheduling-hold: "true"
""",
            )
        )
        self.kubectl(
            "-n",
            namespace,
            "create",
            "configmap",
            "dns-runtime-fixtures",
            f"--from-file=fake_dns.py={HERE / 'fake_dns.py'}",
            f"--from-file=fake_localapi.py={HERE / 'fake_localapi.py'}",
            stdout=subprocess.DEVNULL,
        )
        self.helm(
            "install",
            release,
            str(CHART),
            "--namespace",
            namespace,
            "--values",
            str(values),
            stdout=subprocess.DEVNULL,
        )
        statefulset = self.statefulset(namespace, release)
        template = statefulset["spec"]["template"]["spec"]
        tailscale = next(item for item in template["initContainers"] if item["name"] == "tailscale")
        tailscale["image"] = f"{APP_REPOSITORY}:{APP_TAG}@{APP_DIGEST}"
        tailscale["imagePullPolicy"] = "IfNotPresent"
        tailscale["command"] = ["/bin/sh", "-c"]
        tailscale["args"] = [
            "set -eu\n"
            "mkdir -p /run/status\n"
            "printf '%s\\n' '{\"BackendState\":\"Running\",\"CurrentTailnet\":{\"MagicDNSEnabled\":true,\"MagicDNSSuffix\":\"corp-alpha.ts.net\"},\"Self\":{\"DNSName\":\"gpubox.corp-alpha.ts.net.\",\"TailscaleIPs\":[\"100.64.0.7\"]}}' >/run/status/status.json\n"
            "/usr/sbin/ip address add 100.100.100.100/32 dev lo\n"
            "/usr/bin/python3 -I -B /fixture/fake_dns.py --role magic --host 100.100.100.100 --address 100.64.0.7 --log /run/status/magic.jsonl &\n"
            "exec /usr/bin/python3 -I -B /fixture/fake_localapi.py --socket /var/run/gpubox-tailscale/tailscaled.sock --status /run/status/status.json --log /run/status/localapi.jsonl\n"
        ]
        tailscale["startupProbe"] = {
            "exec": {"command": ["/bin/sh", "-c", "test -S /var/run/gpubox-tailscale/tailscaled.sock"]},
            "periodSeconds": 1,
            "timeoutSeconds": 1,
            "failureThreshold": 30,
        }
        tailscale["volumeMounts"] = [
            mount
            for mount in tailscale.get("volumeMounts", [])
            if mount["name"] == "gpubox-tailscale-socket"
        ] + [
            {"name": "dns-runtime-fixtures", "mountPath": "/fixture", "readOnly": True},
            {"name": "dns-runtime-status", "mountPath": "/run/status"},
        ]
        template["volumes"].extend(
            [
                {"name": "dns-runtime-fixtures", "configMap": {"name": "dns-runtime-fixtures"}},
                {"name": "dns-runtime-status", "emptyDir": {}},
            ]
        )
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "initContainers": template["initContainers"],
                        "volumes": template["volumes"],
                        "nodeSelector": None,
                    }
                }
            }
        }
        self.kubectl(
            "-n",
            namespace,
            "patch",
            "statefulset",
            statefulset["metadata"]["name"],
            "--type=merge",
            "-p",
            json.dumps(patch),
            stdout=subprocess.DEVNULL,
        )
        pending = self.pod(namespace, release)
        self.kubectl("-n", namespace, "delete", "pod", pending["metadata"]["name"], "--wait=true", stdout=subprocess.DEVNULL)
        pod = self.wait_ready(namespace, release)
        name = pod["metadata"]["name"]
        self.wait_corefile(namespace, release, True)
        self.wait_dns_answer(namespace, name, "host.corp-alpha.ts.net.", "100.64.0.7")

        def set_status(payload: str) -> None:
            self.exec(
                namespace,
                name,
                "tailscale",
                "/usr/bin/python3",
                "-c",
                "import pathlib,sys; pathlib.Path('/run/status/status.json').write_text(sys.argv[1])",
                payload,
            )

        enabled = '{"BackendState":"Running","CurrentTailnet":{"MagicDNSEnabled":true,"MagicDNSSuffix":"corp-alpha.ts.net"},"Self":{"DNSName":"gpubox.corp-alpha.ts.net.","TailscaleIPs":["100.64.0.7"]}}'
        invalid_statuses = [
            '{"BackendState":"Running","CurrentTailnet":{"MagicDNSEnabled":false,"MagicDNSSuffix":"corp-alpha.ts.net"},"Self":{"DNSName":"gpubox.corp-alpha.ts.net.","TailscaleIPs":["100.64.0.7"]}}',
            "{malformed",
            '{"BackendState":"NeedsLogin","CurrentTailnet":{"MagicDNSEnabled":true,"MagicDNSSuffix":"corp-alpha.ts.net"},"Self":{"DNSName":"gpubox.corp-alpha.ts.net.","TailscaleIPs":["100.64.0.7"]}}',
        ]
        for index, invalid in enumerate(invalid_statuses):
            set_status(enabled)
            self.wait_corefile(namespace, release, True)
            active_name = f"active-{index}.corp-alpha.ts.net."
            self.wait_dns_answer(namespace, name, active_name, "100.64.0.7")
            set_status(invalid)
            self.wait_corefile(namespace, release, False)
            convergence_name = f"converge-{index}.corp-alpha.ts.net."
            self.wait_dns_answer(namespace, name, convergence_name, "")
            inactive_name = f"inactive-{index}.corp-alpha.ts.net."
            self.exec(
                namespace,
                name,
                "gpubox",
                "/usr/bin/dig",
                "@127.0.0.1",
                inactive_name,
                "A",
                "+tries=1",
                "+time=2",
                "+short",
                check=False,
            )
            wire_log = self.exec(namespace, name, "tailscale", "/usr/bin/cat", "/run/status/magic.jsonl")
            wire_names = [json.loads(line)["qname"].lower() for line in wire_log.splitlines()]
            assert active_name in wire_names
            assert inactive_name not in wire_names

        set_status(enabled)
        self.wait_corefile(namespace, release, True)
        self.wait_dns_answer(namespace, name, "again.corp-alpha.ts.net.", "100.64.0.7")

    def test_missing_python(self) -> None:
        namespace = self.namespace("missing-python")
        release = "missing-python"
        values = self.tmp / "missing-python.yaml"
        values.write_text(
            f"""image:
  repository: {BUSYBOX_REPOSITORY}
  tag: {BUSYBOX_TAG}
  digest: {BUSYBOX_DIGEST}
  pullPolicy: IfNotPresent
command: [/bin/sleep]
args: [infinity]
containerPorts: []
resources: null
sharedMemory:
  enabled: false
service:
  enabled: false
persistence:
  home: {{enabled: false}}
  transfer: {{enabled: false}}
  tmp: {{enabled: false}}
  hostRoot: {{enabled: false}}
tailscale:
  enabled: false
  acceptDNS: false
dns:
  enabled: true
"""
        )
        self.helm("install", release, str(CHART), "--namespace", namespace, "--values", str(values), stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 90
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.pod(namespace, release)
            status = next(
                (item for item in last["status"].get("initContainerStatuses", []) if item["name"] == "dns-controller"),
                None,
            )
            if status and status.get("state", {}).get("waiting", {}).get("reason") in {
                "CreateContainerError",
                "CrashLoopBackOff",
            }:
                break
            time.sleep(1)
        else:
            raise AssertionError(f"missing-Python controller did not fail: {json.dumps(last, indent=2)}")
        assert not any(item["name"] == "gpubox" and item.get("ready") for item in last["status"].get("containerStatuses", []))
        pod_name = last["metadata"]["name"]
        logs = self.kubectl(
            "-n",
            namespace,
            "logs",
            pod_name,
            "-c",
            "dns-controller",
            "--previous",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        assert "/usr/bin/python3 is required" in logs, logs

    def collect_failure(self) -> None:
        destination = pathlib.Path("/tmp/gpubox-dns-implementation-evidence/runtime-tests")
        destination.mkdir(parents=True, exist_ok=True)
        for namespace in self.namespaces:
            for kind in ("pods", "events", "statefulsets"):
                result = self.kubectl("-n", namespace, "get", kind, "-o", "yaml", check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                (destination / f"{namespace}-{kind}.yaml").write_text(result.stdout)

    def cleanup(self) -> None:
        if self.keep:
            print("Retaining namespaces: " + ", ".join(self.namespaces), file=sys.stderr)
        else:
            for namespace in reversed(self.namespaces):
                self.kubectl("delete", "namespace", namespace, "--wait=false", check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=pathlib.Path, required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--namespace-prefix", default=f"gpubox-dns-runtime-{os.getpid()}")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    assert args.kubeconfig.is_file(), args.kubeconfig
    suite = Suite(args.kubeconfig, args.cluster, args.namespace_prefix, args.keep)
    try:
        version = suite.kubectl("version", "-o", "json", stdout=subprocess.PIPE)
        server_version = json.loads(version.stdout)["serverVersion"]["gitVersion"]
        assert server_version == "v1.34.11", server_version
        suite.load_small_images()
        suite.test_current_chart()
        suite.test_controlled_localapi()
        suite.test_legacy_ondelete_upgrade_rollback()
        suite.test_enabled_ondelete_code_freeze()
        suite.test_missing_python()
        print("PASS Kind chart runtime integration")
    except Exception:
        suite.collect_failure()
        raise
    finally:
        suite.cleanup()


if __name__ == "__main__":
    main()
