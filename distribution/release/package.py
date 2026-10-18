#!/usr/bin/env python3
"""Assemble architecture-checked, checksum-pinned offline installer assets.

This step accepts already-built Linux binaries and a Vite build. It never builds
on the deployment node or modifies host services.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("archive", ROOT / "distribution/installer/lib/archive.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def check_elf(path, arch):
    with open(path, "rb") as handle:
        header = handle.read(20)
    if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", header[18:20])[0] != {"amd64": 62, "arm64": 183}[arch]:
        raise ValueError("expected Linux ELF64 " + arch + ": " + str(path))


def runtime_subset(source, destination, arch, lock):
    if archive.digest(source) != lock["archives"][arch]:
        raise ValueError("official runtime archive checksum mismatch")
    executables = {"bin/" + name for name in ("containerd", "containerd-shim-runc-v2", "ctr", "nerdctl", "runc")}
    cni = {"libexec/cni/" + name for name in ("bridge", "loopback", "host-local", "portmap", "firewall", "tuning")}
    selected = set()
    with tarfile.open(source, "r:gz") as handle:
        for member in handle:
            name = member.name.removeprefix("./")
            if name not in executables | cni and not name.startswith("share/doc/") and name not in ("libexec/cni/LICENSE", "libexec/cni/README.md"):
                continue
            name = archive.safe_name(name)
            if not member.isfile():
                if member.isdir():
                    continue
                raise ValueError("selected runtime file is not regular")
            if name in selected:
                raise ValueError("duplicate runtime file")
            selected.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.extractfile(member) as src, open(target, "xb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o755 if name in executables | cni else 0o644)
    if not (executables | cni).issubset(selected):
        raise ValueError("runtime release missing required files")
    for name in executables | cni:
        check_elf(destination / name, arch)


def proxy_subset(source, destination, arch):
    lock=json.loads((ROOT / 'distribution/release/proxy.lock.json').read_text())
    if archive.digest(source)!=lock['archives'][arch]['sha256']:
        raise ValueError('official Caddy archive checksum mismatch')
    destination.mkdir(parents=True)
    with tarfile.open(source,'r:gz') as handle:
        for name,target in [('caddy',destination/'bin/caddy'),('LICENSE',destination/'LICENSE')]:
            member=handle.getmember(name)
            if not member.isfile(): raise ValueError('proxy archive file must be regular')
            target.parent.mkdir(parents=True,exist_ok=True)
            with handle.extractfile(member) as src,open(target,'xb') as dst: shutil.copyfileobj(src,dst)
            target.chmod(0o755 if name=='caddy' else 0o644)
    check_elf(destination/'bin/caddy',arch)
    shutil.copyfile(ROOT/'distribution/release/proxy.lock.json',destination/'proxy.lock.json')


def package(args):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", args.version):
        raise ValueError("invalid version")
    workspace = (ROOT / 'Cargo.toml').read_text().split('[workspace.package]', 1)[1].split('[', 1)[0]
    expected = re.search(r'^version\s*=\s*"([^"]+)"', workspace, re.MULTILINE).group(1)
    if args.version != expected:
        raise ValueError('release version must match the Rust workspace version')
    lock = json.loads((ROOT / "distribution/release/runtime.lock.json").read_text())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloud-release-") as temp:
        stage = Path(temp) / "stage"
        stage.mkdir()
        for binary in ("agentd", "agentctl", "controld"):
            source = Path(args.bin_dir) / binary
            check_elf(source, args.arch)
            target = stage / "bin" / binary
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o755)
        web = Path(args.web_dir)
        if not (web / "index.html").is_file():
            raise ValueError("Web build missing index.html")
        for path in web.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Web build has a linked or special file")
        shutil.copytree(web, stage / "web")
        runtime_subset(Path(args.runtime_archive), stage / "runtime", args.arch, lock)
        proxy_subset(Path(args.proxy_archive or ROOT / ('target/release-cache/caddy_2.11.7_linux_'+args.arch+'.tar.gz')),stage/'proxy',args.arch)
        shutil.copytree(ROOT / "distribution/installer/lib", stage / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "crates/ingress/bootstrap.json", stage / "lib/proxy-bootstrap.json")
        for name in ("install.sh", "cloudctl", "smoke.py", "stateful_smoke.py", "fullstack_smoke.py"):
            shutil.copyfile(ROOT / "distribution/installer" / name, stage / name)
            (stage / name).chmod(0o755)
        shutil.copytree(ROOT / "distribution/installer/systemd", stage / "systemd")
        shutil.copytree(ROOT / "examples", stage / "examples", ignore=shutil.ignore_patterns("go-client", "__pycache__"))
        shutil.copyfile(ROOT / "distribution/release/runtime.lock.json", stage / "runtime.lock.json")
        shutil.copyfile(ROOT / "docs/install.md", stage / "INSTALL.md")
        shutil.copyfile(ROOT / "docs/stateful.md", stage / "STATEFUL.md")
        shutil.copyfile(ROOT / "docs/fullstack.md", stage / "FULLSTACK.md")
        shutil.copyfile(ROOT / "docs/ingress.md", stage / "INGRESS.md")
        shutil.copyfile(ROOT / "docs/upgrade.md", stage / "UPGRADE.md")
        files = {p.relative_to(stage).as_posix(): archive.digest(p) for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"schema_version": 1, "version": args.version, "arch": args.arch, "runtime": lock, "proxy": json.loads((ROOT / "distribution/release/proxy.lock.json").read_text()), "compatibility": json.loads((ROOT / "distribution/release/compatibility.json").read_text()), "files": files}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive.verify(stage, args.arch)
        stem = "deploy-platform-" + args.version + "-linux-" + args.arch
        payload = output / (stem + ".tar.gz")
        with tarfile.open(payload, "w:gz") as handle:
            for p in sorted(stage.rglob("*")):
                handle.add(p, arcname=p.relative_to(stage).as_posix(), recursive=False)
        header = (ROOT / "distribution/installer/bootstrap.sh.in").read_text()
        header = header.replace("@PAYLOAD_SHA256@", archive.digest(payload)).replace("@ARCHIVE_HELPER@", (ROOT / "distribution/installer/lib/archive.py").read_text())
        runner = output / (stem + ".run")
        with open(runner, "wb") as target, open(payload, "rb") as source:
            target.write(header.encode())
            shutil.copyfileobj(source, target)
        runner.chmod(0o755)
        (output / (stem + ".sha256")).write_text("".join(archive.digest(p) + "  " + p.name + "\n" for p in (runner, payload)))
        print(runner)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    p.add_argument("--version", default="0.5.2")
    p.add_argument("--bin-dir", required=True)
    p.add_argument("--web-dir", default=str(ROOT / "apps/web/dist"))
    p.add_argument("--runtime-archive", required=True)
    p.add_argument("--proxy-archive")
    p.add_argument("--output", default=str(ROOT / "target/dist"))
    package(p.parse_args())


if __name__ == "__main__":
    main()
