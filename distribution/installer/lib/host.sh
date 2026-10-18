# shellcheck shell=bash
# HOST_ARCH is an output consumed by the caller.
# shellcheck disable=SC2034
log() { printf '[cloud] %s\n' "$*"; }
die() { printf '[cloud] ERROR: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command missing: $1"; }
preflight_host() {
    [[ $(id -u) == 0 ]] || die 'run this installer with sudo'
    [[ $(uname -s) == Linux ]] || die 'Ubuntu Linux is required'
    # /etc/os-release is trusted system configuration, never bundle content.
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || die 'this release supports Ubuntu 24.04 LTS only'
    [[ -f /sys/fs/cgroup/cgroup.controllers ]] || die 'unified cgroup v2 is required'
    [[ -d /run/systemd/system ]] || die 'systemd must be PID 1 (a regular cloud VM)'
    systemctl show-environment >/dev/null || die 'cannot connect to systemd'
    case $(uname -m) in x86_64) HOST_ARCH=amd64;; aarch64|arm64) HOST_ARCH=arm64;; *) die 'supported architectures: amd64, arm64';; esac
    local available memory
    available=$(df -Pk /opt | awk 'NR==2 {print $4}')
    (( available >= 2097152 )) || die 'at least 2 GiB available disk space is required for staging and images'
    memory=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
    (( memory >= 1572864 )) || die 'at least 1.5 GiB detected RAM is required (target: 2 cores / 2 GiB)'
}
install_dependencies() {
    local missing=() package
    for package in python3 ca-certificates curl iproute2 iptables kmod openssl util-linux; do
        dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed' || missing+=("$package")
    done
    if (( ${#missing[@]} )); then
        [[ $SKIP_APT == 0 ]] || die "missing Ubuntu packages: ${missing[*]}"
        log "installing OS prerequisites: ${missing[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "${missing[@]}"
    fi
    for package in python3 curl ip iptables modprobe openssl flock timeout; do require_command "$package"; done
}
