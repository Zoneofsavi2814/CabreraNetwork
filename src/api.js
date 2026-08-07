import { useCallback, useEffect, useMemo, useState } from "react";

export const EMPTY_DASHBOARD = {
  HISTORY: {
    cpu: [], ram: [], temp: [], ssdPct: [], dnsPerMin: [], loadAvg: [],
    netIn: [], netOut: [], dnsTotal: [], dnsBlocked: [], diskRead: [], diskWrite: [],
  },
  KPIS: [],
  SERVICES: [],
  WEB_APPS: [],
  ADGUARD: { queries: 0, blocked: 0, blockRatio: 0, upstream: "unavailable", topDomains: [], topClients: [], status: "unavailable" },
  K3S: { version: "unavailable", nodes: [], podsByNs: [], events: [], workloads: [] },
  STORAGE: {},
  LOGS: [],
  HOST: {},
  TOPOLOGY: { clients: [], counts: {}, groups: [], aps: [], routerCollector: { state: "loading" } },
  OPS_CENTER: { summary: { status: "unknown", message: "Operations checks are loading" }, cadences: [], events: [] },
  META: { state: "loading", message: "Waiting for live collectors" },
};

const apiFetch = async (url, options = {}) => {
  const res = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `${res.status} ${res.statusText}`);
  }
  return res.json();
};

const mergeDashboard = (base, incoming) => {
  if (!incoming || typeof incoming !== "object") return base;
  const previous = base || EMPTY_DASHBOARD;
  const next = { ...previous };
  for (const [key, value] of Object.entries(incoming)) {
    if (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      previous[key] &&
      typeof previous[key] === "object" &&
      !Array.isArray(previous[key])
    ) {
      next[key] = { ...previous[key], ...value };
    } else {
      next[key] = value;
    }
  }
  return next;
};

export const useDashboardFeed = () => {
  const [data, setData] = useState(null);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(() => new Date());
  const [lastEventAt, setLastEventAt] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const snapshot = await apiFetch("/api/snapshot");
      setData((prev) => mergeDashboard(prev, snapshot));
      setError(null);
      setLastRefresh(new Date());
      return snapshot;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    refresh().catch((err) => {
      if (!active) return;
      setError(err);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [refresh]);

  useEffect(() => {
    const events = new EventSource("/api/events");
    events.onopen = () => {
      setConnected(true);
      setError(null);
    };
    events.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        setData((prev) => mergeDashboard(prev, payload));
        setLastEventAt(new Date());
        setLastRefresh(new Date());
        setConnected(true);
      } catch (err) {
        setError(err);
      }
    };
    events.onerror = () => {
      setConnected(false);
    };
    return () => events.close();
  }, []);

  const stale = useMemo(() => {
    if (!lastEventAt) return true;
    return Date.now() - lastEventAt.getTime() > 8000;
  }, [lastEventAt, lastRefresh]);

  return { data, connected, stale, loading, error, lastRefresh, refresh };
};

export const fetchLogs = ({ sourceType, id, lines = 160, namespace, container, kind }) => {
  const params = new URLSearchParams({
    sourceType,
    id,
    lines: String(lines),
  });
  if (namespace) params.set("namespace", namespace);
  if (container) params.set("container", container);
  if (kind) params.set("kind", kind);
  return apiFetch(`/api/logs?${params}`);
};

export const getSession = () => apiFetch("/api/session");

export const login = (password) => {
  if (typeof window !== "undefined" && window.location.protocol !== "https:") {
    return Promise.reject(new Error("HTTPS is required before entering the Pi4 password."));
  }
  return apiFetch("/api/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
};

export const logout = () => {
  return apiFetch("/api/logout", {
    method: "POST",
    body: JSON.stringify({}),
  });
};

export const postAction = (payload) => {
  return apiFetch("/api/actions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
};

export const getActionJob = (id) => apiFetch(`/api/action-jobs/${encodeURIComponent(id)}`);

export const getTopologyAliases = () => apiFetch("/api/topology/aliases");

export const saveTopologyAlias = (payload) => {
  return apiFetch("/api/topology/aliases", {
    method: "POST",
    body: JSON.stringify(payload),
  });
};
