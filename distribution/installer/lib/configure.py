#!/usr/bin/env python3
"""Generate managed configuration into a staging directory, not the host."""
import argparse
import ipaddress
import json
from pathlib import Path


def generate(destination, cidr):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    subnet = ipaddress.IPv4Network(cidr, strict=True)
    template=Path(__file__).parent / 'proxy-bootstrap.json'
    if not template.exists(): template=Path(__file__).resolve().parents[3] / 'crates/ingress/bootstrap.json'
    proxy=json.loads(template.read_text())
    (root / "proxy-bootstrap.json").write_text(json.dumps(proxy,indent=2)+"\n")
    (root / "containerd.toml").write_text('''version = 3
root = "/var/lib/deploy-platform/containerd"
state = "/run/deploy-platform-containerd/state"
disabled_plugins = ["io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime"]
[grpc]
  address = "/run/deploy-platform-containerd/containerd.sock"
  uid = 0
  gid = 0
[debug]
  level = "warn"
''')
    (root / "nerdctl.toml").write_text('''address = "/run/deploy-platform-containerd/containerd.sock"
namespace = "deploy-platform"
cgroup_manager = "systemd"
cni_path = "/opt/deploy-platform/current/runtime/libexec/cni"
cni_netconfpath = "/etc/deploy-platform/cni"
data_root = "/var/lib/deploy-platform/nerdctl"
''')
    cni = {
        "cniVersion": "1.0.0", "name": "bridge",
        "plugins": [
            {"type": "bridge", "bridge": "cloud0", "isGateway": True, "ipMasq": True, "hairpinMode": True,
             "ipam": {"type": "host-local", "dataDir": "/var/lib/deploy-platform/cni/ipam", "ranges": [[{"subnet": str(subnet), "gateway": str(subnet.network_address + 1)}]], "routes": [{"dst": "0.0.0.0/0"}]}},
            {"type": "portmap", "capabilities": {"portMappings": True}},
            {"type": "firewall", "ingressPolicy": "same-bridge"}, {"type": "tuning"}
        ]
    }
    (root / "10-cloud.conflist").write_text(json.dumps(cni, indent=2) + "\n")
    (root / "nerdctl").write_text('''#!/bin/sh
# Runtime-generated rootfs/config files must remain readable by the image user.
# All parent data directories are private to root; Agent state keeps umask 0077.
umask 022
export PATH=/opt/deploy-platform/current/runtime/bin:/usr/sbin:/usr/bin:/sbin:/bin
export NERDCTL_TOML=/etc/deploy-platform/nerdctl.toml
exec /opt/deploy-platform/current/runtime/bin/nerdctl "$@"
''')


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("destination")
    p.add_argument("cidr")
    args = p.parse_args()
    generate(args.destination, args.cidr)
