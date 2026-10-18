#!/usr/bin/env python3
"""Select a private node bridge without overlapping host routes."""
import argparse
import ipaddress
import json
import sys


def choose(routes, requested=None):
    used = []
    for row in routes:
        if row.get("dev") == "cloud0" or row.get("dst") in (None, "default", "0.0.0.0/0"):
            continue
        try:
            used.append(ipaddress.IPv4Network(row["dst"], strict=False))
        except (ValueError, KeyError):
            continue
    candidates = [requested] if requested else ["10.89.0.0/24", "172.30.240.0/24", "192.168.248.0/24"]
    for candidate in candidates:
        net = ipaddress.IPv4Network(candidate, strict=True)
        private = [ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
        if net.prefixlen != 24 or not any(net.subnet_of(n) for n in private):
            raise ValueError("bridge must be a private /24 network")
        if not any(net.overlaps(route) for route in used):
            return str(net)
    raise ValueError("bridge overlaps host/VPC routes; choose an unused private /24 with --bridge-cidr")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--requested")
    args = p.parse_args()
    print(choose(json.load(sys.stdin), args.requested))
