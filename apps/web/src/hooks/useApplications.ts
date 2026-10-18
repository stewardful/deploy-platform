import { useCallback, useEffect, useRef, useState } from 'react';
import { ControlClient, ControlError, type Application } from '@deploy-platform/control-client';
export function useApplications(client: ControlClient, onUnauthorized: () => void) {
  const [apps, setApps] = useState<Application[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [updated, setUpdated] = useState<Date | null>(null);
  const active = useRef<AbortController>();
  const refresh = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setLoading(true);
    try {
      const value = await client.apps(controller.signal);
      if (!controller.signal.aborted) {
        setApps(value);
        setError('');
        setUpdated(new Date());
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        if (error instanceof ControlError && error.status === 401) onUnauthorized();
        else setError(error instanceof Error ? error.message : '应用列表读取失败');
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [client, onUnauthorized]);
  useEffect(() => {
    void refresh();
    const interval = setInterval(() => {
      if (!document.hidden && navigator.onLine) void refresh();
    }, 10000);
    const visible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', visible);
      active.current?.abort();
    };
  }, [refresh]);
  return { apps, loading, error, updated, refresh };
}
