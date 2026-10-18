import type { AppSpec } from '@deploy-platform/control-client';
/** Stateless descriptions only; never include real secret values. */
export function statefulTemplate(kind: 'postgres' | 'redis', project: string): AppSpec {
  const postgres = kind === 'postgres';
  return {
    schema_version: 1,
    app: { project, name: kind },
    image: postgres
      ? 'docker.io/library/postgres:17-bookworm'
      : 'docker.io/library/redis:7.4-bookworm',
    user: { uid: 999, gid: 999 },
    resources: { memory_mib: postgres ? 384 : 256, cpu_millis: 500, pids: 128 },
    network: 'project',
    ports: [],
    volumes: [{ name: 'data', target: postgres ? '/var/lib/postgresql/data' : '/data' }],
    secrets: [{ name: postgres ? 'db-password' : 'redis-config' }],
    environment: postgres
      ? {
          POSTGRES_PASSWORD_FILE: '/run/secrets/db-password',
          POSTGRES_DB: 'app',
          PGDATA: '/var/lib/postgresql/data/pgdata',
        }
      : {},
    args: postgres
      ? ['postgres', '-c', 'shared_buffers=64MB', '-c', 'max_connections=30']
      : [
          'redis-server',
          '/run/secrets/redis-config',
          '--appendonly',
          'yes',
          '--appendfsync',
          'everysec',
          '--dir',
          '/data',
          '--maxmemory',
          '128mb',
          '--maxmemory-policy',
          'noeviction',
        ],
  };
}
