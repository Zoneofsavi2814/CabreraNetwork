import { useCallback, useEffect, useMemo, useState } from "react";
import { DEFAULT_DASHBOARD } from "./mockData.jsx";

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
  const next = { ...base };
  for (const [key, value] of Object.entries(incoming)) {
    if (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      base[key] &&
      typeof base[key] === "object" &&
      !Array.isArray(base[key])
    ) {
      next[key] = { ...base[key], ...value };
    } else {
      next[key] = value;
    }
  }
  return next;
};

export const useDashboardFeed = () => {
  const [data, setData] = useState(DEFAULT_DASHBOARD);
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

export const fetchLogs = ({ sourceType, id, lines = 160, namespace, container }) => {
  const params = new URLSearchParams({
    sourceType,
    id,
    lines: String(lines),
  });
  if (namespace) params.set("namespace", namespace);
  if (container) params.set("container", container);
  return apiFetch(`/api/logs?${params}`);
};

export const getSession = () => apiFetch("/api/session");

export const login = (password) => {
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
