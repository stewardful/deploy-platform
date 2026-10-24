#!/usr/bin/env python3
"""Node health and opt-in real project smoke test. Never prints the API token."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8088"
NERDCTL = "/opt/deploy-platform/nerdctl"


def request(path, body=None, timeout=8):
    token = Path("/etc/deploy-platform/control.token").read_text().strip()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    # No ambient proxy settings or redirects for the local management channel.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        data = response.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise RuntimeError("oversized management response")
    return json.loads(data)


def wait_ready(seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = request("/api/v1/health")
            if value.get("api_version") == "v1" and value.get("accepting"):
                return value
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent/control API readiness deadline exceeded")
        time.sleep(1)


def runtime(*args):
    return subprocess.check_output([NERDCTL, *args], text=True, timeout=150)


def health(wait):
    wait_ready(wait)
    for unit in ("deploy-containerd", "deploy-agent", "deploy-control", "deploy-proxy"):
        subprocess.run(["systemctl", "is-active", "--quiet", unit], check=True)
    version = runtime("--version").strip()
    runtime("info")
    # A public login page must not imply an unprotected management API.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(BASE + "/", timeout=5) as response:
        if response.status != 200 or "text/html" not in response.headers.get("Content-Type", ""):
            raise RuntimeError("Web login page unavailable")
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(BASE + "/api/v1/apps", timeout=5)
    except urllib.error.HTTPError as error:
        if error.code != 401: raise
    else:
        raise RuntimeError("management API must require authentication")
    print("PASS: dedicated runtime + Agent + control + Web + API authentication; " + version)


def wait_job(job):
    deadline = time.monotonic() + 180
    while job["state"] in ("queued", "running"):
        if time.monotonic() >= deadline:
            raise RuntimeError("job wait timed out; query job " + str(job["id"]) + " before retrying")
        time.sleep(0.4)
        job = request("/api/v1/jobs/" + str(job["id"]))
    if job["state"] != "succeeded":
        error = job.get("error") or {}
        raise RuntimeError("test job failed: " + error.get("code", "unknown") + ": " + error.get("message", "query the job for details"))
    return job


def verify_kernel_limits(instance):
    inspected = json.loads(runtime("inspect", "--", instance))[0]
    pid = inspected['State']['Pid']
    if type(pid) is not int or pid <= 0: raise RuntimeError('test container has no live PID')
    lines = Path('/proc/'+str(pid)+'/cgroup').read_text().splitlines()
    paths = [line[3:] for line in lines if line.startswith('0::')]
    if len(paths) != 1 or not paths[0].startswith('/') or '..' in Path(paths[0]).parts:
        raise RuntimeError('test container has no safe cgroup v2 path')
    group = Path('/sys/fs/cgroup') / paths[0].lstrip('/')
    memory = (group/'memory.max').read_text().strip()
    pids = (group/'pids.max').read_text().strip()
    cpu = (group/'cpu.max').read_text().split()
    if (memory != str(128*1024**2) or pids != '64' or len(cpu) != 2
            or not all(value.isdigit() for value in cpu) or int(cpu[1]) <= 0
            or int(cpu[0])/int(cpu[1]) != 0.25):
        raise RuntimeError('kernel memory/CPU/PID limits did not match')


def project():
    health(15)
    key = "smoke-" + secrets.token_hex(5)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    spec = {"schema_version": 1, "app": {"project": key, "name": "web"},
            "image": "docker.io/nginxinc/nginx-unprivileged:stable-alpine",
            "resources": {"memory_mib": 128, "cpu_millis": 250, "pids": 64},
            "network": "bridge", "ports": [{"host": port, "container": 8080}]}
    submission = {"request_id": key, "spec": spec, "startup_wait_ms": 1000}
    instance = None
    try:
        # No automatic POST retry after an uncertain response. The printed key is non-secret.
        print("test request_id=" + key + "; test project=" + key, flush=True)
        job = wait_job(request("/api/v1/apps/apply", submission))
        instance = job["result"]["instance_id"]
        duplicate = request("/api/v1/apps/apply", submission)
        if duplicate["id"] != job["id"]:
            raise RuntimeError("idempotency mismatch")
        def http_ready():
            deadline = time.monotonic() + 30
            while True:
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open("http://127.0.0.1:" + str(port), timeout=2) as response:
                        if response.status == 200: return
                except OSError: pass
                if time.monotonic() >= deadline: raise RuntimeError("test application HTTP deadline exceeded")
                time.sleep(0.4)
        http_ready()
        inspected = json.loads(runtime("inspect", "--", instance))[0]
        host = inspected["HostConfig"]
        if (host.get("Memory") != 128 * 1024**2 or host.get("PidsLimit") != 64
                or host.get("CpuQuota", 0) / max(host.get("CpuPeriod", 0), 1) != 0.25):
            raise RuntimeError("memory/CPU/PID limits did not match")
        verify_kernel_limits(instance)
        runtime("stop", "--", instance)
        subprocess.run(["systemctl", "restart", "deploy-agent"], check=True)
        wait_ready(30)
        http_ready()
        verify_kernel_limits(instance)
        print("PASS: real image pull, CNI loopback HTTP, resource limits, idempotency and Agent restart recovery")
    finally:
        # Persist stop intent before cleaning this test's owned container. Never prune.
        deployment_failed = sys.exc_info()[0] is not None
        try:
            wait_ready(30)
            stop = request("/api/v1/apps/" + key + "/web/stop", {"request_id": key + "-stop"})
            wait_job(stop)
            if instance:
                data = json.loads(runtime("inspect", "--", instance))[0]
                labels = data.get("Config", {}).get("Labels", {})
                if labels.get("io.deploy-platform.managed-by") == "rust-agent" and labels.get("io.deploy-platform.project") == key:
                    runtime("rm", "--", instance)
            print("test stopped; its stopped metadata remains visible in Web; image cache retained")
        except Exception as cleanup_error:
            print("cleanup incomplete; inspect the dedicated test project " + key + " before retrying", file=sys.stderr)
            # Preserve the original deployment error if both deployment and cleanup fail.
            if deployment_failed:
                print("cleanup error: " + str(cleanup_error), file=sys.stderr)
            else:
                raise


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("health", "project"))
    p.add_argument("--wait", type=int, default=10)
    args = p.parse_args()
    if args.action == "health": health(args.wait)
    else: project()
