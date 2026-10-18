import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ControlClient,
  ControlError,
  isSubmission,
  terminal,
  validJobID,
  type Job,
  type Submission,
} from '@deploy-platform/control-client';
const PENDING = 'cloud.pending.v1';
const IDS = 'cloud.jobs.v1';
function readPending(): { value: Submission | null; damaged: boolean } {
  try {
    const raw = sessionStorage.getItem(PENDING);
    if (!raw) return { value: null, damaged: false };
    const data: unknown = JSON.parse(raw);
    return isSubmission(data) ? { value: data, damaged: false } : { value: null, damaged: true };
  } catch {
    return { value: null, damaged: true };
  }
}
function readIDs(): number[] {
  try {
    const data: unknown = JSON.parse(sessionStorage.getItem(IDS) ?? '[]');
    return Array.isArray(data)
      ? [...new Set(data.filter((id) => typeof id === 'number' && validJobID(String(id))))].slice(
          0,
          50,
        )
      : [];
  } catch {
    return [];
  }
}
export function useTasks(client: ControlClient, onUnauthorized: () => void, onSettled: () => void) {
  const [initial] = useState(readPending);
  const [pending, setPending] = useState<Submission | null>(initial.value);
  const [damaged, setDamaged] = useState(initial.damaged);
  const [ids, setIDs] = useState(readIDs);
  const [jobs, setJobs] = useState<Record<number, Job>>({});
  const [errors, setErrors] = useState<Record<number, string>>({});
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const ignored = useRef(new Set<number>());
  const cursor = useRef(0);
  const submitting = useRef(false);
  const active = useRef(new AbortController());
  useEffect(() => {
    active.current = new AbortController();
    return () => active.current.abort();
  }, []);
  useEffect(() => {
    const retained = new Set(ids);
    setJobs((previous) =>
      Object.fromEntries(Object.entries(previous).filter(([id]) => retained.has(Number(id)))),
    );
    setErrors((previous) =>
      Object.fromEntries(Object.entries(previous).filter(([id]) => retained.has(Number(id)))),
    );
    for (const id of ignored.current) if (!retained.has(id)) ignored.current.delete(id);
  }, [ids]);
  const track = useCallback((job: Job) => {
    setJobs((prev) => ({ ...prev, [job.id]: job }));
    setIDs((prev) => {
      const next = [job.id, ...prev.filter((id) => id !== job.id)].slice(0, 50);
      try {
        sessionStorage.setItem(IDS, JSON.stringify(next));
      } catch {
        /* IDs still remain visible in this page. */
      }
      return next;
    });
  }, []);
  const fetchJob = useCallback(
    async (id: string | number, signal?: AbortSignal) => {
      const job = await client.job(id, signal);
      track(job);
      return job;
    },
    [client, track],
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const poll = async () => {
      if (!document.hidden && navigator.onLine) {
        const candidates = ids.filter(
          (id) =>
            !ignored.current.has(id) && (!jobsRef.current[id] || !terminal(jobsRef.current[id])),
        );
        const selected =
          candidates.length <= 4
            ? candidates
            : Array.from(
                { length: 4 },
                (_, i) => candidates[(cursor.current + i) % candidates.length],
              );
        cursor.current += 4;
        await Promise.all(
          selected.map(async (id) => {
            try {
              const job = await client.job(id, controller.signal);
              if (controller.signal.aborted) return;
              const previous = jobsRef.current[id];
              setJobs((prev) => ({ ...prev, [id]: job }));
              setErrors((prev) => {
                const next = { ...prev };
                delete next[id];
                return next;
              });
              if (terminal(job) && (!previous || !terminal(previous))) onSettled();
            } catch (error) {
              if (controller.signal.aborted) return;
              if (error instanceof ControlError && error.status === 401) {
                onUnauthorized();
                return;
              }
              if (error instanceof ControlError && error.status === 404) ignored.current.add(id);
              setErrors((prev) => ({
                ...prev,
                [id]: error instanceof Error ? error.message : '任务查询失败',
              }));
            }
          }),
        );
      }
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [client, ids, onUnauthorized, onSettled]);
  const send = useCallback(
    async (request: Submission) => {
      if (submitting.current) return null;
      submitting.current = true;
      setSending(true);
      setError('');
      try {
        if (!isSubmission(request))
          throw new Error('配置请求无效或超过 256 KiB，请减少内容后再提交。');
        // Persist before sending so an uncertain POST can be safely replayed after reload.
        try {
          sessionStorage.setItem(PENDING, JSON.stringify(request));
        } catch {
          throw new Error('浏览器无法保存原请求，已阻止提交。请允许本网站的会话存储后再试。');
        }
        setPending(request);
        const job = await client.submit(request, active.current.signal);
        track(job);
        try {
          sessionStorage.removeItem(PENDING);
        } catch {
          /* Retrying a leftover key only returns the same job. */
        }
        setPending(null);
        onSettled();
        return job;
      } catch (error) {
        if (!active.current.signal.aborted) {
          if (error instanceof ControlError && error.status === 401) onUnauthorized();
          if (error instanceof ControlError && [400, 413, 415].includes(error.status)) {
            try {
              sessionStorage.removeItem(PENDING);
            } catch {}
            setPending(null);
          }
          setError(error instanceof Error ? error.message : '提交失败');
        }
        return null;
      } finally {
        if (!active.current.signal.aborted) setSending(false);
        submitting.current = false;
      }
    },
    [client, onSettled, onUnauthorized, track],
  );
  const submit = useCallback(
    async (request: Submission) => {
      if (pending || damaged) {
        setError('请先确认上一次提交的结果，再发起新操作。');
        return null;
      }
      return send(request);
    },
    [pending, damaged, send],
  );
  const discard = useCallback(() => {
    try {
      sessionStorage.removeItem(PENDING);
      setPending(null);
      setDamaged(false);
      setError('');
    } catch {
      setError('无法清除本地记录，请检查浏览器存储权限。');
    }
  }, []);
  return {
    ids,
    jobs,
    errors,
    pending,
    damaged,
    error,
    sending,
    submit,
    retry: () => (pending ? send(pending) : Promise.resolve(null)),
    discard,
    fetchJob,
  };
}
