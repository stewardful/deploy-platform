#!/usr/bin/env bash
# Source-only helper for our disposable systemd-in-Docker test nodes.
# Docker exec may empty cgroup.subtree_control to place its process in the
# container root cgroup. Move the command into init.scope and ask systemd to
# realize the resource hierarchy again before testing nested containers.
node_exec() {
    local options=(exec) node
    while (( $# )); do
        case $1 in
            -i|-t) options+=("$1"); shift;;
            -e|--env) (( $# >= 2 )) || return 2; options+=("$1" "$2"); shift 2;;
            --) shift; break;;
            -*) echo 'unsupported test exec option' >&2; return 2;;
            *) break;;
        esac
    done
    (( $# >= 2 )) || return 2
    node=$1; shift
    [[ $node =~ ^cloud-(installer|upgrade|notification|bootstrap)-test-[0-9]+$ ]] || { echo 'disposable test node required' >&2; return 2; }
    command docker "${options[@]}" "$node" /bin/sh -ec '
        attempts=0
        until [ -f /sys/fs/cgroup/init.scope/cgroup.procs ]; do
            attempts=$((attempts+1)); [ "$attempts" -lt 40 ] || exit 1
            sleep 0.25
        done
        grep -qx "0::/init.scope" /proc/1/cgroup
        printf "%s\n" "$$" > /sys/fs/cgroup/init.scope/cgroup.procs
        until systemctl show-environment >/dev/null 2>&1; do
            attempts=$((attempts+1)); [ "$attempts" -lt 40 ] || exit 1
            sleep 0.25
        done
        systemctl set-property --runtime system.slice TasksAccounting=yes MemoryAccounting=yes
        exec "$@"
    ' cloud-test-command "$@"
}
