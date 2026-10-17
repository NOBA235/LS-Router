import type {
  ClosureResponse,
  CutoffResponse,
  DraftAlertsResponse,
  GraphResponse,
  RerouteResponse,
  ResolveClosureResponse,
  SeasonalRiskResponse,
  WeakLinksResponse,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || "/api";

export class ApiError extends Error {
  readonly statusCode: number;
  readonly code: string;

  constructor(statusCode: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.code = code;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });

  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }

  if (!res.ok) {
    const errObj =
      body && typeof body === "object" && "error" in body
        ? (body as { error?: { code?: string; message?: string } }).error
        : undefined;
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail?: unknown }).detail)
        : undefined;
    const code = errObj?.code || `http_${res.status}`;
    const message = errObj?.message || detail || `Backend HTTP ${res.status} (${res.statusText})`;
    throw new ApiError(res.status, code, message);
  }

  return body as T;
}

export function fetchGraph(includeGeometry = true, signal?: AbortSignal): Promise<GraphResponse> {
  const qs = new URLSearchParams({ include_geometry: String(includeGeometry) });
  return requestJson<GraphResponse>(`/graph?${qs.toString()}`, { method: "GET", signal });
}

export interface CreateClosurePayload {
  description: string;
  lat?: number | null;
  lon?: number | null;
  target_segment_id?: string | null;
  target_bridge_id?: string | null;
  cause?: string | null;
  max_candidates?: number;
  remember_in_history?: boolean;
  started_at?: string | null;
}

export function postClosure(payload: CreateClosurePayload): Promise<ClosureResponse> {
  return requestJson<ClosureResponse>("/closure", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchCutoff(eventId: string, thresholdMin?: number | null): Promise<CutoffResponse> {
  const qs = new URLSearchParams();
  if (thresholdMin != null && Number.isFinite(thresholdMin) && thresholdMin > 0) {
    qs.set("threshold_min", String(thresholdMin));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return requestJson<CutoffResponse>(`/cutoff/${encodeURIComponent(eventId)}${suffix}`, {
    method: "GET",
  });
}

export function fetchReroute(
  fromPlaceId: string,
  toPlaceId: string,
  avoidEventId?: string | null,
): Promise<RerouteResponse> {
  const qs = new URLSearchParams({
    from: fromPlaceId,
    to: toPlaceId,
  });
  if (avoidEventId && avoidEventId.trim()) {
    qs.set("avoid_event_id", avoidEventId.trim());
  }
  return requestJson<RerouteResponse>(`/reroute?${qs.toString()}`, {
    method: "GET",
  });
}

export function fetchWeakLinks(corridorId?: string | null, topK = 5): Promise<WeakLinksResponse> {
  const qs = new URLSearchParams({ top_k: String(topK) });
  if (corridorId && corridorId.trim()) {
    qs.set("corridor_id", corridorId.trim());
  }
  return requestJson<WeakLinksResponse>(`/weak-links?${qs.toString()}`, {
    method: "GET",
  });
}

export function fetchSeasonalRisk(month: number | string): Promise<SeasonalRiskResponse> {
  const qs = new URLSearchParams({ month: String(month) });
  return requestJson<SeasonalRiskResponse>(`/seasonal-risk?${qs.toString()}`, {
    method: "GET",
  });
}

export function postDraftAlerts(eventId: string): Promise<DraftAlertsResponse> {
  return requestJson<DraftAlertsResponse>(`/alerts/${encodeURIComponent(eventId)}`, {
    method: "POST",
  });
}

export function postResolveClosure(
  eventId: string,
  rememberInHistory = false,
): Promise<ResolveClosureResponse> {
  return requestJson<ResolveClosureResponse>(`/resolve/${encodeURIComponent(eventId)}`, {
    method: "POST",
    body: JSON.stringify({ remember_in_history: rememberInHistory }),
  });
}

export function postRememberClosure(
  eventId: string,
): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(`/remember/${encodeURIComponent(eventId)}`, {
    method: "POST",
  });
}

