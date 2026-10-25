"""Installed-node command vocabulary. No application or upgrade transaction logic."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

CLI_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path('/opt/deploy-platform/current')
SOCKET = '/run/deploy-platform/agent.sock'
AGENT = {
    'apps': 'apps', 'deploy': 'apply', 'start': 'start', 'stop': 'stop',
    'logs': 'logs', 'diagnostics': 'diagnostics', 'job': 'job', 'operations': 'operations',
    'validate': 'validate', 'preview': 'plan', 'agent-health': 'doctor', 'maintenance': 'maintenance',
}
GROUPS = {
    'secret': {'list': 'secrets', 'create': 'secret-create'},
    'volume': {'list': 'volumes'},
    'route': {'list': 'routes', 'set': 'route-set', 'delete': 'route-delete'},
}
SERVICES = {'agent': 'deploy-agent', 'control': 'deploy-control', 'runtime': 'deploy-containerd',
            'proxy': 'deploy-proxy', 'update': 'deploy-update', 'install': 'cloudctl-install'}
DESCRIPTION = '''cloudctl — applications and platform maintenance on this node

Applications (Agent API; Go/Web not required):
  apps                         List saved applications
  deploy -f FILE               Deploy an AppSpec; wait for its durable task
  start | stop --project P --name APP
  logs --project P --name APP [--tail N]   Application stdout/stderr only
  diagnostics --project P --name APP     Runtime and HTTP readiness
  job ID | operations          Task result / history
  validate -f FILE             Validate configuration offline
  preview -f FILE              Validated configuration summary; no live diff or dry-run

Data and domains:
  secret list | create         Secret metadata / create from a private file
  volume list                  List application volumes
  route list | set | delete    Manage domain routes with revision checks

Node and updates:
  setup                        Install/update this independent cloudctl executable
  install --channel stable|dev [--run-id ID]   Ensure a verified platform build is installed
  install --file INSTALLER.run [--yes]   Ensure a local verified package is installed
  status                       Check installed services, runtime, Web and authentication
  agent-health                 Check Agent API and task acceptance only
  version                      Platform release, build and component versions
  system-logs SERVICE          agent | control | runtime | proxy | update
  update check | status | policy   Release discovery / task status / local dev policy

Advanced:
  token                        Print private Web login token (sudo)
  maintenance status | enter | exit    Upgrade admission gate, not an upgrade
  test smoke | stateful | fullstack    Real tests; create resources and may restart Agent
  runtime ...                  Direct administrator nerdctl access
  agentctl ...                 Independent Agent CLI escape hatch

Use cloudctl COMMAND --help or cloudctl help COMMAND.
Run management commands with sudo. Upgrades update the platform, not business images.
'''


def execute(args):
    os.execv(args[0], args)


def agent(args, display=None):
    if display and args[-1:] in (['--help'], ['-h']):
        text = subprocess.check_output([str(ROOT/'bin/agentctl'), *args], text=True, timeout=5)
        print(text.replace('agentctl '+args[0], 'cloudctl '+display), end='')
        return
    execute([str(ROOT/'bin/agentctl'), '--socket', SOCKET, *args])


def parser(name, description):
    return argparse.ArgumentParser(prog='cloudctl '+name, description=description, allow_abbrev=False)


def version():
    if not (ROOT/'manifest.json').is_file():
        print(json.dumps({'version': json.loads((CLI_ROOT/'cli.json').read_text())['version'], 'installed': False})); return
    manifest = json.loads((ROOT/'manifest.json').read_text())
    result = {key: manifest.get(key) for key in ('version', 'arch', 'package_kind', 'build')}
    result['cli_version']=json.loads((CLI_ROOT/'cli.json').read_text())['version']
    result['components'] = {}
    for name in ('agentctl', 'agentd'):
        result['components'][name] = subprocess.check_output([str(ROOT/'bin'/name), '--version'], text=True, timeout=5).strip()
    # Go binary's authoritative release identity is the same checked package manifest.
    result['components']['controlplane'] = manifest['version']
    result['components']['runtime'] = {k: v for k, v in manifest.get('runtime', {}).items() if k in ('nerdctl', 'containerd', 'runc', 'cni')}
    result['components']['proxy'] = manifest.get('proxy', {}).get('version')
    print(json.dumps(result, indent=2))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args in (['help'], ['--help'], ['-h']):
        print(DESCRIPTION); return
    if args[0] == 'help':
        if len(args) < 2: raise ValueError('command required')
        args = args[1:]+['--help']
    name, rest = args[0], args[1:]
    if name in ('--version',): name = 'version'
    if name == 'setup':
        parser(name, 'Register this independently packaged cloudctl; use the released executable').parse_args(rest);return
    if name == 'worker':
        if rest or os.geteuid()!=0:raise ValueError('root worker takes no parameters')
        from update_worker import main as work
        work();return
    if name == 'install':
        from first_install import main as install, InstallError
        try: install(rest)
        except InstallError as error:
            print('cloudctl install: '+str(error), file=sys.stderr)
            raise SystemExit(2) from None
        return
    if name in AGENT:
        if name == 'preview' and rest in (['--help'], ['-h']):
            p = parser(name, 'Validate and summarize an AppSpec offline. No live-state comparison, runtime checks or deployment dry-run. Secrets are referenced, not resolved.')
            p.add_argument('-f', '--file', required=True, help='AppSpec JSON or TOML'); p.parse_args(rest); return
        if name == 'logs' and (not rest or not rest[0].startswith('-')):
            raise ValueError('logs requires --project and --name; use system-logs SERVICE for node services')
        if rest in (['--help'], ['-h']):
            agent([AGENT[name], *rest], display=name)
        else:
            agent([AGENT[name], *rest])
        return
    if name in GROUPS:
        if not rest or rest in (['--help'], ['-h']):
            print('Usage: cloudctl '+name+' {'+','.join(GROUPS[name])+'} [options]\nUse cloudctl '+name+' ACTION --help for required options.'); return
        action = rest[0]
        if action not in GROUPS[name]: raise ValueError('unknown '+name+' action')
        if rest[1:] in (['--help'], ['-h']):
            agent([GROUPS[name][action], *rest[1:]], display=name+' '+action)
        else:
            agent([GROUPS[name][action], *rest[1:]])
        return
    if name == 'update':
        from platform_updates import main as updates
        updates([name, *rest]); return
    if name == 'agentctl': agent(rest); return
    if name == 'runtime':
        if not rest or rest in (['--help'], ['-h']):
            print('Usage: cloudctl runtime NERDCTL_ARGS...\nDirect runtime access bypasses Agent desired-state management.'); return
        execute(['/opt/deploy-platform/nerdctl', *rest]); return
    if name == 'test':
        scripts = {'smoke': 'smoke.py', 'stateful': 'stateful_smoke.py', 'fullstack': 'fullstack_smoke.py'}
        if not rest or rest in (['--help'], ['-h']):
            print('Usage: cloudctl test {smoke,stateful,fullstack} [options]\nReal tests create resources; smoke/stateful may restart Agent. Use a test node.'); return
        action, options = rest[0], rest[1:]
        if action not in scripts: raise ValueError('unknown test')
        if action != 'fullstack':
            p = parser('test '+action, 'Run real '+action+' tests; creates resources and may restart Agent.')
            p.parse_args(options)
        execute(['/usr/bin/python3', '-I', '-B', str(ROOT/scripts[action]), *(['project'] if action == 'smoke' else []), *options]); return
    if name == 'system-logs':
        p = parser(name, 'Read the last 100 journal lines of one platform service. Does not read application logs.')
        p.add_argument('service', choices=SERVICES)
        opts = p.parse_args(rest)
        units=['-u','cloudctl-install-*','-u','deploy-update.service'] if opts.service=='install' else ['-u',SERVICES[opts.service]+'.service']
        execute(['/usr/bin/journalctl',*units,'-n','100','--no-pager']);return
    if name in ('status', 'version', 'token'):
        descriptions = {'status': 'Check the full installed node; requires Agent, control, proxy and runtime.',
                        'version': 'Print installed platform manifest and component versions as JSON; no network access.',
                        'token': 'Print the private Web management token. This does not log in or rotate the token.'}
        parser(name, descriptions[name]).parse_args(rest)
        if name == 'status':
            execute(['/usr/bin/python3', '-I', '-B', str(ROOT/'smoke.py'), 'health'])
        elif name == 'version': version()
        else:
            if os.geteuid() != 0: raise ValueError('sudo is required')
            from update_worker import private_read
            print(private_read(Path('/etc/deploy-platform/control.token'), 1024).decode().strip())
        return
    raise ValueError('unknown command; use cloudctl help')


if __name__ == '__main__':
    # -I keeps untrusted PYTHONPATH/current directory off the privileged import path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try: main()
    except ValueError:
        print('cloudctl: invalid command or unavailable configuration/artifact; check cloudctl help and the update guide', file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, subprocess.SubprocessError):
        raise SystemExit('cloudctl failed: check installed services and private configuration; use cloudctl help') from None
