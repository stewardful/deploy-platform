import { validateSpec, type AppSpec } from '@deploy-platform/control-client';
/** Import descriptions only. File bytes stay in the browser until task submission. */
export default function SpecImport({
  onImport,
  onError,
}: {
  onImport: (spec: AppSpec) => void;
  onError: (message: string) => void;
}) {
  return (
    <label className="spec-import">
      导入应用 JSON
      <input
        type="file"
        accept=".json,application/json"
        onChange={async (e) => {
          const file = e.currentTarget.files?.[0];
          e.currentTarget.value = '';
          if (!file) return;
          if (file.size > 65536) {
            onError('应用配置最多 64 KiB');
            return;
          }
          try {
            const spec: AppSpec = JSON.parse(await file.text());
            const problem = validateSpec(spec);
            if (problem) {
              onError(problem);
              return;
            }
            onImport(spec);
          } catch {
            onError('配置无效，请选择完整的应用 JSON 文件');
          }
        }}
      />
    </label>
  );
}
