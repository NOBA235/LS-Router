import { useCallback, useEffect, useState } from "react";

type Health = {
  status: string;
  falkordb: { connected: boolean; query_ok: boolean; graph: string; error: string | null };
};
type State = { kind: "checking" } | { kind: "unreachable" } | { kind: "done"; ok: boolean; body: Health };

export default function BackendStatus() {
  const [state, setState] = useState<State>({ kind: "checking" });

  const check = useCallback(async (signal?: AbortSignal) => {
    setState({ kind: "checking" });
    try {
      const res = await fetch("/api/health", { signal });
      setState({ kind: "done", ok: res.ok, body: await res.json() });
    } catch (err) {
      if ((err as Error).name !== "AbortError") setState({ kind: "unreachable" });
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    check(ctrl.signal);
    return () => ctrl.abort();
  }, [check]);

  let text = "Checking backend…";
  let cls = "pending";
  if (state.kind === "unreachable") {
    text = "Backend API not reachable (is uvicorn running on port 8000?)";
    cls = "bad";
  } else if (state.kind === "done") {
    const db = state.body.falkordb;
    cls = state.ok ? "good" : "bad";
    text = state.ok
      ? `Backend OK. FalkorDB answered a query on graph "${db.graph}".`
      : `Backend up, FalkorDB not OK${db?.error ? `: ${db.error}` : ""}`;
  }

  return (
    <div className={`status ${cls}`}>
      <span>{text}</span>
      <button type="button" onClick={() => check()}>
        Recheck
      </button>
    </div>
  );
}
