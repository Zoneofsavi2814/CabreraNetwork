/* Cabrera Network dashboard — composes top bar, KPI strip, charts, services, AdGuard,
   k3s, Storage, footer. Plus ⌘K palette, confirm modal, toast stack,
   refresh ripple, KPI detail. */

import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Icon } from "./icons.jsx";
import { Sparkline, TimeSeries, Donut, HBar, StackedBar, PodsBar } from "./charts.jsx";
import { DEFAULT_DASHBOARD } from "./mockData.jsx";
import {
  fetchLogs,
  getActionJob,
  getSession,
  login,
  logout,
  postAction,
  saveTopologyAlias,
  useDashboardFeed,
} from "./api.js";

let MOCK = DEFAULT_DASHBOARD;

// ------------------------------------------------------------- helpers
const useNow = () => {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
};

const fmtTime = (d) =>
  `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`;

const asNumber = (value, fallback = 0) => {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
};

const fmtKbps = (value) => {
  const kbps = asNumber(value);
  if (kbps >= 1000) return `${(kbps / 1000).toFixed(kbps >= 10000 ? 0 : 1)} Mbps`;
  return `${kbps.toFixed(kbps >= 100 ? 0 : 1)} Kbps`;
};

const truncate = (value, max = 18) => {
  const text = String(value || "");
  return text.length > max ? `${text.slice(0, Math.max(0, max - 3))}...` : text;
};

const useMediaQuery = (query) => {
  const [matches, setMatches] = useState(() => (
    typeof window === "undefined" ? false : window.matchMedia(query).matches
  ));

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);

  return matches;
};

const MOBILE_SECTIONS = [
  { id: "overview", label: "Overview", icon: "activity" },
  { id: "network", label: "Network", icon: "network" },
  { id: "services", label: "Services", icon: "brandCubes" },
  { id: "ops", label: "Ops", icon: "command" },
  { id: "logs", label: "Logs", icon: "logs" },
];

// ------------------------------------------------------------- toasts
function useToasts() {
  const [items, setItems] = useState([]);
  const push = useCallback((msg, tone = "cyan") => {
    const id = Math.random().toString(36).slice(2);
    setItems((it) => [...it, { id, msg, tone }]);
    setTimeout(() => setItems((it) => it.filter(t => t.id !== id)), 3200);
  }, []);
  const ui = (
    <div className="toast-stack">
      {items.map(t => (
        <div key={t.id} className="toast" style={{ borderLeftColor: `var(--${t.tone})` }}>
          <Icon name="info" size={14} style={{ color: `var(--${t.tone})` }} />
          <span style={{ color: "var(--fg-dim)" }}>{t.msg}</span>
        </div>
      ))}
    </div>
  );
  return { push, ui };
}

const LoginScreen = ({ loading, error, onSubmit }) => {
  const [password, setPassword] = useState("");
  const disabled = loading || password.length === 0;
  return (
    <main className="login-shell">
      <form
        className="login-card"
        onSubmit={(event) => {
          event.preventDefault();
          if (!disabled) onSubmit(password);
        }}
      >
        <span className="chip cyan login-mark"><Icon name="brandShield" /></span>
        <div>
          <h1>Enter Password</h1>
          <p>Cabrera Network</p>
        </div>
        <input
          className="login-input"
          autoFocus
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="Pi4 login password"
          autoComplete="current-password"
        />
        {error && <div className="login-error">{error}</div>}
        <button className="btn login-submit" type="submit" disabled={disabled}>
          <Icon name="check" />
          {loading ? "Checking" : "Unlock"}
        </button>
      </form>
    </main>
  );
};

const AuthGate = ({ children }) => {
  const [state, setState] = useState({ loading: true, authenticated: false, error: "" });

  useEffect(() => {
    let active = true;
    getSession()
      .then((session) => {
        if (active) setState({ loading: false, authenticated: Boolean(session.authenticated), error: "" });
      })
      .catch(() => {
        if (active) setState({ loading: false, authenticated: false, error: "" });
      });
    return () => {
      active = false;
    };
  }, []);

  const handleLogin = async (password) => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const session = await login(password);
      setState({ loading: false, authenticated: Boolean(session.authenticated), error: "" });
    } catch (err) {
      setState({ loading: false, authenticated: false, error: "Password did not unlock the dashboard." });
    }
  };

  const handleLogout = async () => {
    await logout().catch(() => {});
    setState({ loading: false, authenticated: false, error: "" });
  };

  if (state.loading && !state.authenticated) {
    return <LoginScreen loading error="" onSubmit={() => {}} />;
  }
  if (!state.authenticated) {
    return <LoginScreen loading={state.loading} error={state.error} onSubmit={handleLogin} />;
  }
  return children({ onLogout: handleLogout });
};

// ------------------------------------------------------------- Icon Legend overlay
// A floating, tooltip-style panel that documents the icon system.
const IconLegend = ({ open, onClose }) => {
  if (!open) return null;
  const statusRows = [
    { tone: "ok",   label: "Healthy",   desc: "service running normally" },
    { tone: "warn", label: "Degraded",  desc: "elevated errors / restarts" },
    { tone: "fail", label: "Failed",    desc: "service is down or unreachable" },
    { tone: "cyan", label: "Interactive", desc: "user-initiated / live signal" },
    { tone: "muted",label: "Muted",     desc: "informational / inactive" },
  ];
  const actions = [
    { name: "restart", label: "Restart" },
    { name: "stop",    label: "Stop" },
    { name: "play",    label: "Start" },
    { name: "logs",    label: "View logs" },
    { name: "external",label: "Open UI" },
    { name: "refresh", label: "Refresh" },
    { name: "search",  label: "Search" },
    { name: "more",    label: "More" },
  ];
  const brand = [
    { name: "brandShield",    label: "DNS / AdGuard Home" },
    { name: "brandCubes",     label: "Kubernetes / k3s" },
    { name: "brandFolderNet", label: "Samba / NAS share" },
    { name: "brandTerminal",  label: "SSH" },
    { name: "brandSocket",    label: "Socket / API listener" },
    { name: "brandHeartbeat", label: "Uptime Kuma" },
  ];
  return (
    <div className="icon-legend" style={{
      position: "fixed", top: 64, right: 16, zIndex: 55,
      width: 340, maxHeight: "calc(100vh - 80px)", overflow: "auto",
      background: "linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%)",
      border: "1px solid var(--hairline-strong)",
      borderRadius: 12,
      boxShadow: "0 0 0 1px rgba(34,211,238,0.15), inset 0 1px 0 rgba(255,255,255,0.03)",
    }}>
      <div style={{
        display: "flex", alignItems: "center", padding: "10px 14px",
        borderBottom: "1px solid var(--hairline)",
      }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 500 }}>Icon legend</div>
          <div style={{ fontSize: 10, color: "var(--slate-2)" }}>32px chip · 1px status ring · 16px glyph</div>
        </div>
        <button className="chip-btn" onClick={onClose}><Icon name="x" /></button>
      </div>

      <Section title="Status rings">
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 12px", alignItems: "center" }}>
          {statusRows.map(s => (
            <React.Fragment key={s.tone}>
              <span className={`chip ${s.tone}`}><Icon name="check" /></span>
              <div>
                <div style={{ fontSize: 12 }}>{s.label}</div>
                <div style={{ fontSize: 10, color: "var(--slate-2)" }}>{s.desc}</div>
              </div>
            </React.Fragment>
          ))}
        </div>
      </Section>

      <Section title="Brand glyphs">
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 10px", alignItems: "center" }}>
          {brand.map(b => (
            <React.Fragment key={b.name}>
              <span className="chip ok"><Icon name={b.name} /></span>
              <span style={{ fontSize: 11, color: "var(--fg-dim)" }}>{b.label}</span>
            </React.Fragment>
          ))}
        </div>
      </Section>

      <Section title="Action chips" last>
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 10px", alignItems: "center" }}>
          {actions.map(a => (
            <React.Fragment key={a.name}>
              <span className="chip-btn"><Icon name={a.name} /></span>
              <span style={{ fontSize: 11, color: "var(--fg-dim)" }}>{a.label}</span>
            </React.Fragment>
          ))}
        </div>
      </Section>
    </div>
  );
};

const Section = ({ title, children, last }) => (
  <div style={{ padding: "10px 14px", borderBottom: last ? "none" : "1px solid var(--hairline)" }}>
    <div className="panel-title" style={{ marginBottom: 8 }}>{title}</div>
    {children}
  </div>
);

// ------------------------------------------------------------- top bar
const TopBar = ({ onOpenPalette, onOpenLegend, onToggleDensity, onToggleTheme, onLogout, density, theme, alertCount, refreshing, host, feed }) => {
  const now = useNow();
  const hostName = host?.name || "pi4";
  const hostIp = host?.ip || "192.168.0.101";
  const uptime = host?.uptime || "unknown";
  const feedTone = feed?.connected && !feed?.stale ? "ok" : feed?.connected ? "warn" : "fail";
  return (
    <div className="topbar">
      <div className="stage" style={{
        height: 56, display: "flex", alignItems: "center", gap: 16,
      }}>
        {/* Left — mark + breadcrumb */}
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div className="breathe" style={{
            width: 28, height: 28, borderRadius: 7,
            border: "1px solid rgba(34,211,238,0.45)",
            background: "linear-gradient(135deg, rgba(34,211,238,0.28), rgba(129,140,248,0.16))",
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "var(--cyan-strong)",
            position: "relative",
          }}>
            <span className="mono" style={{ fontSize: 11, fontWeight: 600 }}>π</span>
          </div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
            <span className="mono" style={{ fontSize: 13, fontWeight: 500, color: "var(--fg)" }}>{hostName}</span>
            <span style={{ color: "var(--slate-3)" }}>/</span>
            <span style={{ fontSize: 12, color: "var(--slate)" }}>Cabrera Network</span>
            <span style={{ color: "var(--slate-3)" }}>/</span>
            <span style={{ fontSize: 12, color: "var(--fg-dim)" }}>overview</span>
          </div>
          <span className="mono" style={{ fontSize: 11, color: "var(--slate-2)", marginLeft: 6 }}>
            {hostIp}
          </span>
        </div>

        {/* Center — clock + uptime */}
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className={`dot ${feedTone}`} />
            <span className="mono" style={{ fontSize: 14, color: "var(--fg)", letterSpacing: "0.04em" }}>
              {fmtTime(now)}
            </span>
          </div>
          <span className="pill">
            <Icon name="clock" size={11} />
            <span className="mono">uptime {uptime}</span>
          </span>
        </div>

        {/* Right — alerts, density, theme, ⌘K */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button className="chip-btn" title="Alerts" style={{ position: "relative" }}>
            <Icon name="bell" />
            {alertCount > 0 && (
              <span style={{
                position: "absolute", top: -4, right: -4,
                minWidth: 14, height: 14, padding: "0 3px",
                borderRadius: 7, background: "var(--amber)", color: "#000",
                fontSize: 9, fontWeight: 700,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontFamily: "var(--mono)",
              }}>{alertCount}</span>
            )}
          </button>
          <div style={{ display: "flex", border: "1px solid var(--hairline-strong)", borderRadius: 6, overflow: "hidden" }}>
            <button className="btn ghost" style={{ height: 28, borderRadius: 0, border: "none",
              background: density === "normal" ? "rgba(34,211,238,0.08)" : "transparent",
              color: density === "normal" ? "var(--cyan)" : "var(--slate)",
            }} onClick={() => onToggleDensity("normal")}>Normal</button>
            <button className="btn ghost" style={{ height: 28, borderRadius: 0, border: "none",
              background: density === "compact" ? "rgba(34,211,238,0.08)" : "transparent",
              color: density === "compact" ? "var(--cyan)" : "var(--slate)",
            }} onClick={() => onToggleDensity("compact")}>Compact</button>
          </div>
          <button className="chip-btn" title="Theme" onClick={onToggleTheme}>
            <Icon name="moon" />
          </button>
          <button className="chip-btn" title="Icon legend" onClick={onOpenLegend}>
            <Icon name="info" />
          </button>
          <button className="chip-btn" title="Logout" onClick={onLogout}>
            <Icon name="x" />
          </button>
          <button className="btn" onClick={onOpenPalette} style={{ paddingLeft: 8, paddingRight: 6 }}>
            <Icon name="search" size={13} />
            <span style={{ color: "var(--slate)", marginRight: 22 }}>Search…</span>
            <span className="kbd">⌘K</span>
          </button>
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- KPI strip
// Each KPI gets its own blue-family hue so the strip reads as a spectrum;
// a warn/fail status overrides to amber/rose.
const KPI_HUE = { cpu: "cyan", ram: "sky", temp: "indigo", ssd: "violet", dns: "teal" };

const KPICard = ({ kpi, ticked, onClick }) => {
  const num = typeof kpi.value === "number"
    ? (kpi.value % 1 === 0 ? kpi.value : kpi.value.toFixed(1))
    : kpi.value;
  const tone = kpi.status === "warn" ? "amber" : kpi.status === "fail" ? "rose" : (KPI_HUE[kpi.id] || "cyan");
  const toneVar = `var(--${tone})`;
  return (
    <div
      className="panel"
      style={{
        padding: "10px 12px", cursor: "pointer",
        background: `radial-gradient(150% 130% at 100% 0%, var(--${tone}-soft), transparent 58%), linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%)`,
        boxShadow: `inset 0 2px 0 0 ${toneVar}`,
      }}
      onClick={onClick}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span className={`chip ${kpi.status === "ok" ? "ok" : kpi.status === "warn" ? "warn" : "fail"}`}>
          <Icon name={kpi.glyph} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontSize: 10, letterSpacing: "0.08em",
            textTransform: "uppercase", color: "var(--slate)",
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
          }}>{kpi.label}</div>
        </div>
        <span className={`delta ${kpi.deltaTone}`}>{kpi.delta}</span>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginBottom: 2 }}>
        <span
          className="mono"
          style={{
            fontSize: 22, fontWeight: 500, letterSpacing: -0.4, color: "var(--fg)",
            transition: ticked ? "color 240ms" : undefined,
          }}
        >{num}</span>
        {kpi.suffix && <span className="mono" style={{ fontSize: 12, color: "var(--slate)" }}>{kpi.suffix}</span>}
      </div>
      {kpi.sub && <div style={{ fontSize: 10, color: "var(--slate-2)", marginBottom: 4 }}>{kpi.sub}</div>}
      <Sparkline values={kpi.history} color={toneVar} />
    </div>
  );
};

const KPIStrip = ({ kpis, tickedId, onSelect }) => (
  <div className="kpi-strip">
    {kpis.map(k => (
      <KPICard key={k.id} kpi={k} ticked={k.id === tickedId} onClick={() => onSelect(k)} />
    ))}
  </div>
);

// ------------------------------------------------------------- services
const ServiceRow = ({ svc, onAction }) => {
  const [menu, setMenu] = useState(false);
  return (
    <div className="row-hover" style={{
      display: "flex", alignItems: "center", gap: 10,
      padding: "10px 16px",
      borderBottom: "1px solid var(--hairline)",
      position: "relative",
    }}>
      <span className={`chip ${svc.status === "ok" ? "ok" : svc.status === "warn" ? "warn" : "fail"}`}>
        <Icon name={svc.glyph} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 500, color: "var(--fg)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{svc.label}</span>
          <span className={`dot ${svc.status}`} style={{ marginLeft: 4, flexShrink: 0 }} />
        </div>
        <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)",
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>{svc.unit}</div>
      </div>
      <span className="mono" style={{ fontSize: 11, color: "var(--slate)", whiteSpace: "nowrap" }}>:{svc.port}</span>
      <div className="row-secondary" style={{ display: "flex", gap: 4 }}>
        {svc.ui && (
          <button className="chip-btn" title="Open UI" onClick={() => onAction("open", svc)}>
            <Icon name="external" />
          </button>
        )}
        <button className="chip-btn warn" title="Restart" onClick={() => onAction("restart", svc)}>
          <Icon name="restart" />
        </button>
        <button className="chip-btn" title="More" onClick={(e) => { e.stopPropagation(); setMenu(m => !m); }}>
          <Icon name="more" />
        </button>
        {menu && (
          <>
            <div onClick={() => setMenu(false)} style={{ position: "fixed", inset: 0, zIndex: 40 }} />
            <div style={{
              position: "absolute", top: 38, right: 14, zIndex: 41,
              background: "var(--elev)", border: "1px solid var(--hairline-strong)",
              borderRadius: 8, minWidth: 160, padding: 4,
            }}>
              {[
                ["Restart", "restart", "warn"],
                ["View logs", "logs", null],
                svc.ui && ["Open UI", "open", null],
              ].filter(Boolean).map(([label, kind, tone]) => (
                <button key={kind} className="btn ghost" style={{
                  width: "100%", justifyContent: "flex-start",
                  border: "none", height: 30,
                  color: tone === "warn" ? "var(--amber)" : "var(--fg-dim)",
                }} onClick={() => { setMenu(false); onAction(kind, svc); }}>
                  <Icon name={kind === "restart" ? "restart" : kind === "logs" ? "logs" : "external"} />
                  {label}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
};

const ServicesPanel = ({ services, onAction }) => (
  <div className="panel" style={{ display: "flex", flexDirection: "column" }}>
    <div className="panel-head">
      <div className="panel-title">Services</div>
      <span className="pill ok"><span className="dot ok" /> {services.filter(s => s.status === "ok").length} / {services.length} healthy</span>
    </div>
    <div style={{ display: "flex", flexDirection: "column" }}>
      {services.map(s => <ServiceRow key={s.id} svc={s} onAction={onAction} />)}
    </div>
  </div>
);

const WebAppsPanel = ({ apps = [] }) => (
  <div className="panel">
    <div className="panel-head">
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="chip cyan"><Icon name="external" /></span>
        <div>
          <div style={{ fontSize: 13, fontWeight: 500 }}>Web Apps</div>
          <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>{apps.length} LAN links</div>
        </div>
      </div>
      <span className="pill cyan"><Icon name="globe" size={11} /> Cabrera Network</span>
    </div>
    <div className="webapps-grid" style={{ padding: "var(--s-5)", display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10 }}>
      {apps.map((app) => (
        <a
          key={app.id}
          href={app.url}
          target="_blank"
          rel="noreferrer"
          className="row-hover"
          style={{
            display: "grid",
            gridTemplateColumns: "auto minmax(0, 1fr) auto",
            alignItems: "center",
            gap: 10,
            minHeight: 58,
            padding: "10px 12px",
            border: "1px solid var(--hairline)",
            borderRadius: 10,
            background: "rgba(148,163,184,0.02)",
            textDecoration: "none",
            color: "var(--fg)",
          }}
        >
          <span className={`chip ${app.status === "ok" ? "ok" : "warn"}`}><Icon name={app.glyph || "external"} /></span>
          <span style={{ minWidth: 0 }}>
            <span style={{ display: "block", fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{app.label}</span>
            <span className="mono" style={{ display: "block", marginTop: 2, fontSize: 10, color: "var(--slate-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {app.url}
            </span>
          </span>
          <span className={`pill ${app.status === "ok" ? "ok" : "warn"}`} style={{ height: 20, fontSize: 10 }}>
            {app.statusLabel || app.kind || "link"}
          </span>
        </a>
      ))}
      {apps.length === 0 && <span style={{ color: "var(--slate-2)", fontSize: 11 }}>No web apps discovered</span>}
    </div>
  </div>
);

// ------------------------------------------------------------- charts row
const ChartsGrid = () => (
  <div className="charts-grid">
    <ChartPanel
      title="CPU + Load"
      legend={[
        { label: "cpu %", color: "var(--cyan)" },
        { label: "load×10", color: "var(--indigo)" },
      ]}
      seriesList={[
        { values: MOCK.HISTORY.cpu, color: "var(--cyan)" },
        { values: MOCK.HISTORY.loadAvg.map(v => v * 10), color: "var(--indigo)", area: false },
      ]}
    />
    <ChartPanel
      title="Network · kbps"
      legend={[
        { label: "in", color: "var(--sky)" },
        { label: "out", color: "var(--teal)" },
      ]}
      seriesList={[
        { values: MOCK.HISTORY.netIn,  color: "var(--sky)" },
        { values: MOCK.HISTORY.netOut, color: "var(--teal)" },
      ]}
    />
    <ChartPanel
      title="DNS · total / blocked"
      legend={[
        { label: "total", color: "var(--cyan)" },
        { label: "blocked", color: "var(--rose)", kind: "bar" },
      ]}
      seriesList={[{ values: MOCK.HISTORY.dnsTotal, color: "var(--cyan)" }]}
      bars={{ values: MOCK.HISTORY.dnsBlocked, color: "var(--rose)" }}
    />
    <ChartPanel
      title="Disk I/O · MB/s"
      legend={[
        { label: "read",  color: "var(--teal)" },
        { label: "write", color: "var(--indigo)" },
      ]}
      seriesList={[
        { values: MOCK.HISTORY.diskRead,  color: "var(--teal)" },
        { values: MOCK.HISTORY.diskWrite, color: "var(--indigo)" },
      ]}
    />
  </div>
);

const ChartPanel = ({ title, legend, seriesList, bars }) => (
  <div className="panel chart-panel">
    <div className="panel-head" style={{ padding: "6px 12px", minHeight: 28 }}>
      <div className="panel-title" style={{ fontSize: 10 }}>{title}</div>
      <div style={{ display: "flex", gap: 10 }}>
        {legend.map(l => (
          <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 9, color: "var(--slate)", whiteSpace: "nowrap" }}>
            {l.kind === "bar"
              ? <span style={{ width: 7, height: 7, background: l.color, opacity: 0.6, borderRadius: 1 }} />
              : <span style={{ width: 12, height: 2, background: l.color, borderRadius: 1 }} />}
            {l.label}
          </div>
        ))}
      </div>
    </div>
    <div style={{ padding: "6px 4px 2px" }}>
      <TimeSeries seriesList={seriesList} bars={bars} height={96} />
    </div>
  </div>
);

// ------------------------------------------------------------- AdGuard HERO
// Full-width band sitting at the very top of main content. Big meter shows
// block-rate as the dial; the live qps ticks in the center.
const AdGuardHero = () => {
  const a = MOCK.ADGUARD;
  // Live qps: derived from the dnsPerMin history, animated so it feels alive.
  const [qps, setQps] = useState(5.1);
  useEffect(() => {
    let i = 0;
    const t = setInterval(() => {
      const v = MOCK.HISTORY.dnsPerMin[i % MOCK.HISTORY.dnsPerMin.length] / 60;
      setQps(Math.max(0.2, v));
      i++;
    }, 1100);
    return () => clearInterval(t);
  }, []);
  const maxDom = Math.max(...a.topDomains.map(d => d.count));
  const maxCli = Math.max(...a.topClients.map(c => c.count));

  return (
    <div className="panel adguard-hero" style={{ background: "linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%)" }}>
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip ok"><Icon name="brandShield" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>AdGuard Home</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>AdGuardHome.service · :53 · :8080</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="pill">
            <span style={{ color: "var(--slate-2)" }}>upstream</span>
            <span className="mono" style={{ color: "var(--cyan)" }}>{a.upstream}</span>
          </span>
          <span className="pill ok"><span className="dot ok" /> active</span>
          <button className="chip-btn" title="Open admin UI"><Icon name="external" /></button>
        </div>
      </div>
      <div className="adguard-hero-grid" style={{
        display: "grid", gridTemplateColumns: "240px 1.2fr 1.2fr 0.8fr",
        gap: 20, padding: "12px 16px",
      }}>
        {/* Big live meter */}
        <BlockRateMeter blockRatio={a.blockRatio} blocked={a.blocked} queries={a.queries} qps={qps} />
        {/* Top blocked domains */}
        <div>
          <div className="panel-title" style={{ marginBottom: 8 }}>Top blocked domains</div>
          <HBar items={a.topDomains.map(d => ({ name: d.name, count: d.count }))} max={maxDom} color="var(--rose)" />
        </div>
        {/* Top clients */}
        <div>
          <div className="panel-title" style={{ marginBottom: 8 }}>Top clients</div>
          <HBar items={a.topClients} max={maxCli} color="var(--cyan)" />
        </div>
        {/* Today stats column */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="panel-title">Today</div>
          <StatLine label="Queries"   value={a.queries.toLocaleString()} />
          <StatLine label="Blocked"   value={a.blocked.toLocaleString()} tone="rose" />
          <StatLine label="Avg qps"   value={(a.queries / 86400).toFixed(2)} />
          <StatLine label="Peak qps"  value="9.8" />
          <StatLine label="Filters"   value="12" />
          <StatLine label="Lists"     value="5" />
        </div>
      </div>
    </div>
  );
};

const StatLine = ({ label, value, tone }) => (
  <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", borderBottom: "1px solid var(--hairline)", paddingBottom: 4 }}>
    <span style={{ fontSize: 10, color: "var(--slate)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
    <span className="mono" style={{ fontSize: 13, color: tone === "rose" ? "var(--rose)" : "var(--fg)" }}>{value}</span>
  </div>
);

// Big circular block-rate meter with live qps in the center
const BlockRateMeter = ({ blockRatio, blocked, queries, qps }) => {
  const size = 200;
  const stroke = 12;
  const r = (size - stroke - 14) / 2;
  const cx = size / 2, cy = size / 2;
  const c = 2 * Math.PI * r;
  const pct = blockRatio / 100;
  // tick marks around the meter
  const ticks = Array.from({ length: 60 }).map((_, i) => i);
  return (
    <div style={{ position: "relative", width: size, height: size, margin: "0 auto" }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <defs>
          <linearGradient id="meterGrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--cyan)" />
            <stop offset="55%" stopColor="var(--sky)" />
            <stop offset="100%" stopColor="var(--indigo)" />
          </linearGradient>
        </defs>
        {/* outer tick ring */}
        <g>
          {ticks.map(i => {
            const a = (i / ticks.length) * Math.PI * 2 - Math.PI / 2;
            const isMajor = i % 5 === 0;
            const inner = r + stroke / 2 + 4;
            const outer = inner + (isMajor ? 5 : 2.5);
            const x1 = cx + Math.cos(a) * inner;
            const y1 = cy + Math.sin(a) * inner;
            const x2 = cx + Math.cos(a) * outer;
            const y2 = cy + Math.sin(a) * outer;
            return <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} stroke={isMajor ? "rgba(56,189,248,0.4)" : "rgba(120,180,220,0.22)"} strokeWidth={isMajor ? 1.2 : 0.8} />;
          })}
        </g>
        {/* track */}
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(56,189,248,0.12)" strokeWidth={stroke} />
        {/* progress */}
        <circle
          cx={cx} cy={cy} r={r}
          fill="none" stroke="url(#meterGrad)" strokeWidth={stroke}
          strokeDasharray={`${c * pct} ${c}`}
          strokeDashoffset={c / 4}
          strokeLinecap="round"
          transform={`rotate(-90 ${cx} ${cy})`}
          style={{ filter: "drop-shadow(0 0 6px var(--cyan-glow))", transition: "stroke-dasharray 600ms cubic-bezier(.16,1,.3,1)" }}
        />
        {/* needle dot at progress end */}
        <circle
          cx={cx + Math.cos(pct * 2 * Math.PI - Math.PI / 2) * r}
          cy={cy + Math.sin(pct * 2 * Math.PI - Math.PI / 2) * r}
          r={4}
          fill="var(--cyan-strong)"
          style={{ filter: "drop-shadow(0 0 5px var(--cyan-glow))" }}
        />
      </svg>
      <div style={{
        position: "absolute", inset: 0, display: "flex",
        flexDirection: "column", alignItems: "center", justifyContent: "center",
        gap: 0, pointerEvents: "none",
      }}>
        <div style={{ fontSize: 9, color: "var(--slate)", textTransform: "uppercase", letterSpacing: "0.10em", marginBottom: 2 }}>live qps</div>
        <div className="mono breathe" style={{ fontSize: 34, fontWeight: 500, letterSpacing: -1, color: "var(--cyan-strong)", lineHeight: 1 }}>
          {qps.toFixed(1)}
        </div>
        <div className="mono" style={{ fontSize: 11, color: "var(--slate)", marginTop: 4 }}>
          <span style={{ color: "var(--rose)" }}>{blockRatio.toFixed(1)}%</span>
          <span style={{ color: "var(--slate-3)" }}> blocked</span>
        </div>
        <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)", marginTop: 2 }}>
          {blocked.toLocaleString()} <span style={{ color: "var(--slate-3)" }}>/</span> {queries.toLocaleString()}
        </div>
      </div>
    </div>
  );
};

// (Legacy AdGuardPanel retained below for reference, no longer rendered.)
const AdGuardPanel = () => {
  const a = MOCK.ADGUARD;
  const maxDom = Math.max(...a.topDomains.map(d => d.count));
  const maxCli = Math.max(...a.topClients.map(c => c.count));
  return (
    <div className="panel">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip ok"><Icon name="brandShield" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>AdGuard Home</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>AdGuardHome.service · :53 · :8080</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="pill"><span style={{ color: "var(--slate-2)" }}>upstream</span><span className="mono" style={{ color: "var(--cyan)" }}>{a.upstream}</span></span>
          <span className="pill ok"><span className="dot ok" /> active</span>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr 1fr", gap: 24, padding: "var(--s-5)" }}>
        {/* Donut */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
          <Donut value={a.blocked} total={a.queries}
            color="var(--rose)"
            label={`${a.blockRatio}%`}
            sublabel="block ratio" />
          <div style={{ display: "flex", gap: 16 }}>
            <div style={{ textAlign: "center" }}>
              <div className="mono" style={{ fontSize: 16, color: "var(--fg)" }}>{a.queries.toLocaleString()}</div>
              <div style={{ fontSize: 10, color: "var(--slate)", letterSpacing: "0.08em", textTransform: "uppercase" }}>queries</div>
            </div>
            <div style={{ textAlign: "center" }}>
              <div className="mono" style={{ fontSize: 16, color: "var(--rose)" }}>{a.blocked.toLocaleString()}</div>
              <div style={{ fontSize: 10, color: "var(--slate)", letterSpacing: "0.08em", textTransform: "uppercase" }}>blocked</div>
            </div>
          </div>
        </div>
        {/* Top blocked domains */}
        <div>
          <div className="panel-title" style={{ marginBottom: 12 }}>Top blocked domains</div>
          <HBar items={a.topDomains.map(d => ({ name: d.name, count: d.count }))} max={maxDom} color="var(--rose)" />
        </div>
        {/* Top clients */}
        <div>
          <div className="panel-title" style={{ marginBottom: 12 }}>Top clients</div>
          <HBar items={a.topClients} max={maxCli} color="var(--cyan)" />
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- k3s
const K3sPanel = () => {
  const k = MOCK.K3S;
  return (
    <div className="panel">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip ok"><Icon name="brandCubes" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>k3s · {k.version}</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>k3s.service · single-node</div>
          </div>
        </div>
        <span className="pill ok"><span className="dot ok" /> control-plane ready</span>
      </div>
      <div className="k3s-grid" style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1.6fr", gap: 24, padding: "var(--s-5)" }}>
        {/* Nodes table */}
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>Nodes</div>
          <div style={{ border: "1px solid var(--hairline)", borderRadius: 8, overflow: "hidden" }}>
            <div style={{
              display: "grid", gridTemplateColumns: "1fr 1.4fr 1fr 0.6fr 0.6fr",
              fontSize: 10, color: "var(--slate)", textTransform: "uppercase", letterSpacing: "0.08em",
              background: "rgba(148,163,184,0.04)",
              padding: "8px 12px", borderBottom: "1px solid var(--hairline)",
            }}>
              <span>Name</span><span>Role</span><span>Version</span><span>Ready</span><span>Age</span>
            </div>
            {k.nodes.map(n => (
              <div key={n.name} style={{
                display: "grid", gridTemplateColumns: "1fr 1.4fr 1fr 0.6fr 0.6fr",
                padding: "10px 12px", alignItems: "center", fontSize: 12,
              }}>
                <span className="mono" style={{ color: "var(--fg)" }}>{n.name}</span>
                <span className="mono" style={{ color: "var(--slate)", fontSize: 11 }}>{n.role}</span>
                <span className="mono" style={{ color: "var(--slate)", fontSize: 11 }}>{n.version}</span>
                <span><span className="pill ok"><Icon name="check" size={10}/> Ready</span></span>
                <span className="mono" style={{ color: "var(--slate)" }}>{n.age}</span>
              </div>
            ))}
          </div>
        </div>
        {/* Pods by namespace */}
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>Pods by namespace</div>
          <PodsBar items={k.podsByNs} />
          <div style={{ display: "flex", gap: 12, marginTop: 4, fontSize: 10, color: "var(--slate)" }}>
            <span><span style={{ display: "inline-block", width: 8, height: 8, background: "var(--emerald)", marginRight: 4, borderRadius: 2 }} />running</span>
            <span><span style={{ display: "inline-block", width: 8, height: 8, background: "var(--amber)",   marginRight: 4, borderRadius: 2 }} />pending</span>
            <span><span style={{ display: "inline-block", width: 8, height: 8, background: "var(--rose)",    marginRight: 4, borderRadius: 2 }} />failed</span>
          </div>
        </div>
        {/* Events */}
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>Recent events</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 160, overflow: "auto", paddingRight: 4 }}>
            {k.events.map((e, i) => (
              <div key={i} style={{
                display: "grid", gridTemplateColumns: "auto auto 1fr",
                gap: 8, alignItems: "baseline", fontSize: 11,
                padding: "6px 8px", borderRadius: 6,
                background: "rgba(148,163,184,0.03)",
              }}>
                <span className="mono" style={{ color: "var(--slate-2)" }}>{e.t}</span>
                <span className="pill ok" style={{ height: 18, fontSize: 9, padding: "0 6px" }}>{e.reason}</span>
                <span style={{ color: "var(--fg-dim)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  <span className="mono" style={{ color: "var(--slate)" }}>{e.obj}</span>{" "}
                  {e.msg}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- Topology
const GROUP_META = [
  { id: "wired", label: "Wired LAN", glyph: "plug", tone: "cyan", x: 112 },
  { id: "basement", label: "Basement AP", glyph: "mesh", tone: "ok", x: 286 },
  { id: "loft", label: "Loft AP", glyph: "mesh", tone: "ok", x: 460 },
  { id: "wifi-unknown", label: "Wi-Fi AP unknown", glyph: "wifi", tone: "warn", x: 634 },
  { id: "unknown", label: "Link unknown", glyph: "question", tone: "warn", x: 808 },
];

const LINK_OPTIONS = [
  ["", "Auto"],
  ["wired", "Wired"],
  ["wifi", "Wi-Fi"],
  ["2.4g", "2.4G"],
  ["5g", "5G"],
  ["6g", "6G"],
  ["lan-observed", "LAN observed"],
  ["unknown", "Unknown"],
];

const AP_OPTIONS = [
  ["", "Auto"],
  ["main", "Main router"],
  ["basement", "Basement AP"],
  ["loft", "Loft AP"],
  ["unknown", "Unknown AP"],
];

const linkLabel = (value) => {
  const key = String(value || "").toLowerCase();
  return {
    wired: "Wired",
    wifi: "Wi-Fi",
    "2.4g": "2.4G",
    "5g": "5G",
    "6g": "6G",
    "lan-observed": "LAN observed",
    unknown: "Unknown",
  }[key] || value || "Unknown";
};

const linkTone = (value) => {
  const key = String(value || "").toLowerCase();
  if (key === "wired") return "cyan";
  if (["wifi", "2.4g", "5g", "6g"].includes(key)) return "ok";
  if (key === "lan-observed") return "warn";
  return "muted";
};

const routerCollectorTone = (collector) => {
  if (collector?.status === "ok") return "ok";
  if (collector?.state === "stale" || collector?.state === "not_polled") return "warn";
  return "muted";
};

const routerCollectorLabel = (collector) => {
  if (!collector) return "Router pending";
  const apClientCount = Object.values(collector.apClientCounts || {}).reduce((sum, value) => sum + asNumber(value), 0);
  const suffix = apClientCount ? ` · AP ${apClientCount}` : "";
  if (collector.status === "ok") return `Router/AP live · ${collector.clientCount || 0}${suffix}`;
  if (collector.state === "stale") return `Router/AP stale · ${collector.clientCount || 0}${suffix}`;
  if (collector.state === "missing_credentials") return "Router creds needed";
  if (collector.state === "unavailable") return "Router unavailable";
  return "Router pending";
};

const iconForDeviceGlyph = (glyph) => {
  if (["phone", "laptop", "tv", "cpu", "router", "globe", "network"].includes(glyph)) return glyph;
  if (glyph === "iot") return "zap";
  return "network";
};

const groupIdFor = (client) => {
  if (["basement", "loft"].includes(client.apId)) return client.apId;
  if (client.linkType === "wired") return "wired";
  if (["wifi", "2.4g", "5g", "6g"].includes(client.linkType)) return "wifi-unknown";
  return "unknown";
};

const deriveGroups = (clients) => GROUP_META.map((meta) => {
  const rows = clients.filter((client) => (client.groupId || groupIdFor(client)) === meta.id);
  return {
    id: meta.id,
    label: meta.label,
    count: rows.length,
    online: rows.filter((row) => row.online).length,
    rxKbps: rows.reduce((sum, row) => sum + asNumber(row.rxKbps), 0),
    txKbps: rows.reduce((sum, row) => sum + asNumber(row.txKbps), 0),
  };
});

const apLabel = (id, aps = []) => {
  const ap = aps.find((row) => row.apId === id || row.id === id);
  if (ap) return ap.displayName || ap.name || id;
  return AP_OPTIONS.find(([value]) => value === id)?.[1] || "Auto";
};

const TopologyPanel = ({ onSaved, onToast }) => {
  const topology = MOCK.TOPOLOGY || {};
  const router = topology.router || { name: "router", ip: "192.168.0.1" };
  const host = topology.host || MOCK.HOST || { name: "pi4", ip: "192.168.0.101" };
  const aps = Array.isArray(topology.aps) ? topology.aps : [];
  const clients = Array.isArray(topology.clients) ? topology.clients : [];
  const groups = Array.isArray(topology.groups) && topology.groups.length ? topology.groups : deriveGroups(clients);
  const counts = topology.counts || {};
  const routerCollector = topology.routerCollector || {};
  const devices = [...aps.map((ap) => ({ ...ap, isAp: true, groupId: ap.apId || "ap" })), ...clients].slice(0, 72);
  const collectorTone = routerCollectorTone(routerCollector);

  return (
    <div className="panel">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip cyan"><Icon name="network" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>Network topology</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>
              {counts.online || 0} online · {counts.known || counts.total || clients.length} known · {topology.source || "Pi collectors"}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <span className="pill cyan"><Icon name="activity" size={11} /> {fmtKbps(topology.aggregateKbps)}</span>
          <span className="pill"><span className="mono">{counts.apsOnline || 0}/{counts.aps || aps.length}</span> APs</span>
          <span className="pill"><span className="mono">{counts.wired || 0}</span> wired</span>
          <span className="pill ok"><span className="mono">{counts.wifi || 0}</span> Wi-Fi</span>
          <span className="pill warn"><span className="mono">{counts.unknown || 0}</span> unknown</span>
          <span className={`pill ${collectorTone}`} title={routerCollector.lastError || routerCollector.state || ""}>
            <Icon name={collectorTone === "ok" ? "check" : "info"} size={11} /> {routerCollectorLabel(routerCollector)}
          </span>
        </div>
      </div>
      <div className="topology-layout" style={{ padding: "8px 12px 12px" }}>
        <TopologyGraph router={router} host={host} aps={aps} clients={clients} groups={groups} />
        <TopologyDeviceTable devices={devices} aps={aps} onSaved={onSaved} onToast={onToast} />
      </div>
    </div>
  );
};

const isWifiLink = (value) => ["wifi", "2.4g", "5g", "6g"].includes(String(value || "").toLowerCase());

const graphParentIdFor = (client) => {
  const apId = String(client.apId || "").toLowerCase();
  const link = String(client.linkType || client.interface || "").toLowerCase();
  if (["basement", "loft"].includes(apId)) return apId;
  if (apId === "main") return "main";
  if (link === "wired" || isWifiLink(link)) return "main";
  return "unplaced";
};

const sortTopologyRows = (rows) => [...rows].sort((a, b) => {
  const aScore = (a.online ? 1000000 : 0) + asNumber(a.rxKbps) + asNumber(a.txKbps);
  const bScore = (b.online ? 1000000 : 0) + asNumber(b.rxKbps) + asNumber(b.txKbps);
  return bScore - aScore;
});

const TopologyGraph = ({ router, host, aps, clients }) => {
  const VBW = 920;
  const VBH = 438;
  const parentY = 76;
  const childStartY = 178;
  const childRowGap = 54;
  const unplacedY = 382;
  const apSlots = [
    { id: "basement", fallback: "Basement AP", x: 468 },
    { id: "loft", fallback: "Loft AP", x: 744 },
  ];
  const hostRow = {
    key: "host-pi4",
    displayName: host.name || "pi4",
    name: host.name || "pi4",
    ip: host.ip || MOCK.HOST.ip,
    linkType: "wired",
    glyph: "cpu",
    online: true,
    rxKbps: 0,
    txKbps: 0,
    _host: true,
  };
  const clientsByParent = { main: [hostRow], basement: [], loft: [], unplaced: [] };
  clients.forEach((client) => {
    const parentId = graphParentIdFor(client);
    clientsByParent[parentId].push(client);
  });
  Object.keys(clientsByParent).forEach((key) => {
    clientsByParent[key] = sortTopologyRows(clientsByParent[key]);
  });

  const parentStats = (rows) => ({
    total: rows.length,
    online: rows.filter((row) => row.online).length,
    wired: rows.filter((row) => row.linkType === "wired").length,
    wifi: rows.filter((row) => isWifiLink(row.linkType)).length,
    kbps: rows.reduce((sum, row) => sum + asNumber(row.rxKbps) + asNumber(row.txKbps), 0),
  });

  const mainStats = parentStats(clientsByParent.main.filter((row) => !row._host));
  const parentNodes = [
    {
      id: "main",
      x: 192,
      y: parentY,
      label: router.name || "Archer BE400",
      sub: `${mainStats.wired} wired · ${mainStats.wifi} Wi-Fi`,
      glyph: "router",
      tone: "cyan",
      rows: clientsByParent.main,
      limit: 6,
    },
    ...apSlots.map((slot) => {
      const ap = aps.find((row) => row.apId === slot.id || row.id === slot.id);
      const stats = parentStats(clientsByParent[slot.id]);
      return {
        id: slot.id,
        x: slot.x,
        y: parentY,
        label: ap?.displayName || ap?.name || slot.fallback,
        sub: `${stats.online}/${stats.total} clients · ${ap?.ip || "configured"}`,
        glyph: "mesh",
        tone: ap ? (ap.online ? "ok" : "warn") : "muted",
        rows: clientsByParent[slot.id],
        limit: 6,
      };
    }),
  ];

  const uplinkEdges = parentNodes
    .filter((node) => node.id !== "main")
    .map((node) => ({
      id: `uplink-${node.id}`,
      d: `M ${parentNodes[0].x + 66} ${parentY} C ${(parentNodes[0].x + node.x) / 2} ${parentY - 26}, ${(parentNodes[0].x + node.x) / 2} ${parentY + 26}, ${node.x - 66} ${parentY}`,
      tone: "uplink",
    }));

  const clientNodes = parentNodes.flatMap((parent) => {
    const rows = parent.rows.slice(0, parent.limit);
    return rows.map((client, index) => {
      const col = rows.length === 1 ? 0 : index % 2;
      const row = Math.floor(index / 2);
      return {
        ...client,
        parent,
        x: parent.x + (rows.length === 1 ? 0 : col === 0 ? -48 : 48),
        y: childStartY + row * childRowGap,
      };
    });
  });

  const clientEdges = clientNodes.map((client, index) => ({
    id: `client-${client.parent.id}-${index}`,
    d: `M ${client.parent.x} ${client.parent.y + 28} C ${client.parent.x} ${client.y - 76}, ${client.x} ${client.y - 60}, ${client.x} ${client.y - 24}`,
    tone: client.parent.id === "main" ? "direct" : "ap",
  }));

  const moreLabels = parentNodes
    .map((node) => ({ ...node, hidden: Math.max(0, node.rows.length - node.limit) }))
    .filter((node) => node.hidden > 0);

  const unplacedRows = clientsByParent.unplaced;
  const unplacedVisible = unplacedRows.slice(0, 5);

  return (
    <svg className="topology-graph" viewBox={`0 0 ${VBW} ${VBH}`} width="100%" preserveAspectRatio="xMidYMid meet" style={{ display: "block", minHeight: 380 }}>
      {[...uplinkEdges, ...clientEdges].map((edge) => (
        <path
          key={edge.id}
          d={edge.d}
          fill="none"
          stroke={edge.tone === "uplink" ? "rgba(148,163,184,0.26)" : edge.tone === "ap" ? "rgba(45,212,191,0.3)" : "rgba(34,211,238,0.28)"}
          strokeWidth={edge.tone === "uplink" ? "1.2" : "1"}
          strokeDasharray={edge.tone === "uplink" ? "6 5" : "3 3"}
        />
      ))}
      {[...uplinkEdges, ...clientEdges].map((edge, i) => (
        <g key={`dots-${edge.id}`}>
          <circle r="3" fill={edge.tone === "ap" ? "var(--emerald)" : "var(--cyan)"} style={{ offsetPath: `path('${edge.d}')`, animation: `flow-out 3.1s linear ${i * 0.16}s infinite` }} />
          <circle r="2" fill={edge.tone === "ap" ? "var(--emerald)" : "var(--cyan)"} opacity="0.48" style={{ offsetPath: `path('${edge.d}')`, animation: `flow-out 3.1s linear ${i * 0.16 + 1.55}s infinite` }} />
        </g>
      ))}

      {parentNodes.map((node) => (
        <g key={node.id}>
          <TopoNode x={node.x} y={node.y} label={node.label} sub={node.sub} glyph={node.glyph} tone={node.tone} />
          <text x={node.x} y={node.y + 42} textAnchor="middle" fill="var(--slate-2)" fontSize="9" fontFamily="var(--mono)">
            {fmtKbps(parentStats(node.rows).kbps)}
          </text>
        </g>
      ))}

      {clientNodes.map((client) => (
        <TopoNode
          key={`${client.key || client.mac || client.ip || client.name}`}
          x={client.x}
          y={client.y}
          label={client.displayName || client.name || client.ip}
          sub={linkLabel(client.linkType)}
          glyph={client.glyph || "device"}
          tone={client.tone || (client.online ? "ok" : "warn")}
          small
        />
      ))}

      {moreLabels.map((node) => (
        <text key={`more-${node.id}`} x={node.x} y={childStartY + Math.ceil(node.limit / 2) * childRowGap + 8} textAnchor="middle" fill="var(--slate-2)" fontSize="9" fontFamily="var(--mono)">
          +{node.hidden} more
        </text>
      ))}

      <g>
        <rect x="54" y={unplacedY - 24} width="812" height="58" rx="8" fill="rgba(7,11,22,0.72)" stroke="rgba(251,191,36,0.24)" />
        <text x="74" y={unplacedY - 3} fill="var(--amber)" fontSize="10" fontFamily="var(--mono)">
          Unplaced observations · {unplacedRows.filter((row) => row.online).length}/{unplacedRows.length} online
        </text>
        <text x="74" y={unplacedY + 14} fill="var(--slate-2)" fontSize="9" fontFamily="var(--mono)">
          LAN-observed or DNS-only devices without a router/AP link
        </text>
        {unplacedVisible.map((client, index) => (
          <TopoNode
            key={`unplaced-${client.key || client.mac || client.ip || client.name}`}
            x={450 + index * 84}
            y={unplacedY + 5}
            label={client.displayName || client.name || client.ip}
            sub={linkLabel(client.linkType)}
            glyph={client.glyph || "device"}
            tone="muted"
            small
          />
        ))}
      </g>
    </svg>
  );
};

const TopologyDeviceTable = ({ devices, aps, onSaved, onToast }) => {
  const [editingKey, setEditingKey] = useState(null);
  const [draft, setDraft] = useState({ alias: "", location: "", linkType: "", apId: "" });
  const [saving, setSaving] = useState(false);

  const startEdit = (device) => {
    setEditingKey(device.key);
    setDraft({
      alias: device.alias || "",
      location: device.manualLocation || "",
      linkType: device.manualLinkType || "",
      apId: device.manualApId || "",
    });
  };

  const save = async (clear = false) => {
    if (!editingKey) return;
    setSaving(true);
    try {
      await saveTopologyAlias({
        key: editingKey,
        alias: clear ? "" : draft.alias,
        location: clear ? "" : draft.location,
        linkType: clear ? "" : draft.linkType,
        apId: clear ? "" : draft.apId,
      });
      onToast?.(clear ? "Topology alias cleared" : "Topology alias saved", "cyan");
      setEditingKey(null);
      await onSaved?.();
    } catch (err) {
      onToast?.(`Topology save failed: ${err.message}`, "rose");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="topology-device-table" style={{ minWidth: 0, border: "1px solid var(--hairline)", borderRadius: 8, overflow: "hidden", alignSelf: "stretch" }}>
      <div className="topology-table-head">
        <span>Device</span><span>IP</span><span>Link</span><span>AP</span><span>Source</span><span />
      </div>
      <div style={{ maxHeight: 390, overflow: "auto" }}>
        {devices.length === 0 ? (
          <div style={{ padding: 18, color: "var(--slate-2)", fontSize: 12 }}>Waiting for LAN clients</div>
        ) : devices.map((device) => (
          <TopologyDeviceRow
            key={`${device.key || device.mac || device.ip || device.name}`}
            device={device}
            aps={aps}
            editing={editingKey === device.key}
            draft={draft}
            saving={saving}
            onEdit={() => startEdit(device)}
            onDraft={setDraft}
            onCancel={() => setEditingKey(null)}
            onSave={() => save(false)}
            onClear={() => save(true)}
          />
        ))}
      </div>
    </div>
  );
};

const TopologyDeviceRow = ({ device, aps, editing, draft, saving, onEdit, onDraft, onCancel, onSave, onClear }) => {
  const link = device.linkType || "unknown";
  const name = device.displayName || device.name || device.ip || "unknown";
  const sourceName = device.sourceName && device.sourceName !== name ? device.sourceName : "";
  return (
    <div className="row-hover" style={{ borderBottom: "1px solid var(--hairline)" }}>
      <div className="topology-table-row">
        <div style={{ minWidth: 0, display: "flex", alignItems: "center", gap: 8 }}>
          <span className={`dot ${device.online ? "ok" : "warn"}`} />
          <span className={`chip ${device.isAp ? "ok" : linkTone(link)}`} style={{ width: 26, height: 26 }}>
            <Icon name={device.isAp ? "network" : iconForDeviceGlyph(device.glyph)} size={13} />
          </span>
          <div style={{ minWidth: 0 }}>
            <div className="mono" title={name} style={{ color: "var(--fg)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</div>
            <div className="mono" style={{ fontSize: 9, color: "var(--slate-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {sourceName || device.mac || device.key || "-"}
            </div>
          </div>
        </div>
        <span className="mono" style={{ color: "var(--slate)", fontSize: 11 }}>{device.ip || "-"}</span>
        <span><TopologyPill tone={linkTone(link)} label={linkLabel(link)} /></span>
        <span className="mono" style={{ color: "var(--fg-dim)", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {device.isAp ? (device.location || apLabel(device.apId, aps)) : apLabel(device.apId, aps)}
        </span>
        <span className="mono" style={{ color: "var(--slate-2)", fontSize: 10, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {device.confidence || "unknown"} · {device.sourceText || "-"}
        </span>
        <button className="chip-btn" title="Edit" onClick={onEdit} disabled={!device.key}>
          <Icon name="more" />
        </button>
      </div>
      {editing && (
        <div className="topology-editor">
          <input
            className="topology-input"
            value={draft.alias}
            placeholder={device.sourceName || name}
            onChange={(e) => onDraft((d) => ({ ...d, alias: e.target.value }))}
          />
          <input
            className="topology-input"
            value={draft.location}
            placeholder={device.location || "location"}
            onChange={(e) => onDraft((d) => ({ ...d, location: e.target.value }))}
          />
          <select className="topology-select" value={draft.linkType} onChange={(e) => onDraft((d) => ({ ...d, linkType: e.target.value }))}>
            {LINK_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select className="topology-select" value={draft.apId} onChange={(e) => onDraft((d) => ({ ...d, apId: e.target.value }))}>
            {AP_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <span style={{ flex: 1 }} />
          <button className="btn ghost" disabled={saving} onClick={onCancel}>Cancel</button>
          <button className="btn ghost" disabled={saving} onClick={onClear}>Clear</button>
          <button className="btn" disabled={saving} onClick={onSave}><Icon name="check" />Save</button>
        </div>
      )}
    </div>
  );
};

const TopologyPill = ({ tone, label }) => (
  <span className={`pill ${tone}`} style={{ height: 18, padding: "0 6px", fontSize: 10 }}>{label}</span>
);

const TopoNode = ({ x, y, label, sub, glyph, tone = "ok", small }) => {
  const w = small ? 84 : 126;
  const h = small ? 38 : 50;
  const ringColor = tone === "ok" ? "rgba(45,212,191,0.55)" :
                    tone === "warn" ? "rgba(251,191,36,0.55)" :
                    tone === "cyan" ? "rgba(34,211,238,0.55)" :
                    "rgba(148,163,184,0.34)";
  const iconColor = tone === "ok" ? "var(--emerald)" : tone === "warn" ? "var(--amber)" : tone === "cyan" ? "var(--cyan)" : "var(--slate)";
  return (
    <g transform={`translate(${x - w/2}, ${y - h/2})`}>
      <rect x="0" y="0" width={w} height={h} rx="8" fill="#0a1020" stroke={ringColor} strokeWidth="1" />
      <g transform="translate(8, 8)">
        <rect x="0" y="0" width={small ? 20 : 24} height={small ? 20 : 24} rx="5" fill="#070b16" stroke={ringColor} strokeWidth="1" />
        <g transform={small ? "translate(2, 2)" : "translate(4, 4)"} color={iconColor} stroke={iconColor} fill="none" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <TopoGlyph name={glyph} />
        </g>
      </g>
      <text x={small ? 34 : 40} y={small ? 17 : 20} fill="var(--fg)" fontSize={small ? "10" : "11"} fontFamily="var(--mono)">
        {truncate(label, small ? 8 : 15)}
      </text>
      <text x={small ? 34 : 40} y={small ? 29 : 34} fill="var(--slate-2)" fontSize="8.5" fontFamily="var(--mono)">
        {truncate(sub, small ? 8 : 16)}
      </text>
    </g>
  );
};

const TopoGlyph = ({ name }) => {
  const map = {
    router: <><rect x="0" y="9" width="16" height="6" rx="1"/><line x1="4" y1="12" x2="4.01" y2="12"/><line x1="7" y1="12" x2="7.01" y2="12"/><path d="M4 7a4 4 0 0 1 8 0"/><path d="M2 5a6 6 0 0 1 12 0"/></>,
    phone:  <><rect x="3" y="1" width="10" height="14" rx="1.5"/><line x1="7" y1="13" x2="9" y2="13"/></>,
    laptop: <><rect x="2" y="2" width="12" height="8" rx="1"/><path d="M0 13h16l-1-2H1z"/></>,
    tv:     <><rect x="1" y="2" width="14" height="9" rx="1"/><line x1="6" y1="14" x2="10" y2="14"/></>,
    cpu:    <><rect x="3" y="3" width="10" height="10" rx="1"/><rect x="5.5" y="5.5" width="5" height="5"/><line x1="5" y1="0" x2="5" y2="3"/><line x1="11" y1="0" x2="11" y2="3"/><line x1="5" y1="13" x2="5" y2="16"/><line x1="11" y1="13" x2="11" y2="16"/><line x1="0" y1="5" x2="3" y2="5"/><line x1="0" y1="11" x2="3" y2="11"/><line x1="13" y1="5" x2="16" y2="5"/><line x1="13" y1="11" x2="16" y2="11"/></>,
    globe:  <><circle cx="8" cy="8" r="6"/><line x1="2" y1="8" x2="14" y2="8"/><path d="M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></>,
    mesh:   <><circle cx="8" cy="3" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="13" cy="12" r="2"/><path d="M7 5 4 10M9 5l3 5M5 12h6"/></>,
    iot:    <><path d="M4 5a4 4 0 0 1 8 0"/><rect x="4" y="6" width="8" height="8" rx="2"/><path d="M7 10h2"/></>,
    plug:   <><path d="M6 1v4M10 1v4"/><path d="M4 5h8v3a4 4 0 0 1-8 0z"/><path d="M8 12v3"/></>,
    wifi:   <><path d="M2 6a9 9 0 0 1 12 0"/><path d="M4.5 8.5a5.5 5.5 0 0 1 7 0"/><path d="M7 11a2 2 0 0 1 2 0"/><circle cx="8" cy="14" r="1"/></>,
    question: <><circle cx="8" cy="8" r="7"/><path d="M6 6a2.5 2.5 0 1 1 4 2c-1 .7-1.5 1.1-1.5 2.2"/><line x1="8.5" y1="13" x2="8.5" y2="13.01"/></>,
    device: <><rect x="3" y="3" width="10" height="10" rx="2"/><circle cx="8" cy="8" r="2"/><path d="M8 0v3M8 13v3M0 8h3M13 8h3"/></>,
  };
  return map[name] || map.device;
};

// ------------------------------------------------------------- Storage
const StoragePanel = () => {
  const s = MOCK.STORAGE;
  return (
    <div className="panel">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip ok"><Icon name="disk" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>Storage</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>2 mounts · ext4</div>
          </div>
        </div>
      </div>
      <div style={{ padding: "var(--s-5)", display: "flex", flexDirection: "column", gap: 18 }}>
        <StorageRow label="/" mount="/" used={s.root.used} total={s.root.total}
          segments={[{ label: "root", value: s.root.used, tone: "cyan" }]} />
        <StorageRow label="/mnt/ssd" mount="/mnt/ssd" used={s.ssd.used} total={s.ssd.total}
          segments={s.ssd.segments} showLegend />
      </div>
    </div>
  );
};

const StorageRow = ({ label, mount, used, total, segments, showLegend }) => {
  const pct = (used / total) * 100;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
        <span className="mono" style={{ fontSize: 13, color: "var(--fg)" }}>{label}</span>
        <span style={{ fontSize: 11, color: "var(--slate-2)" }}>ext4</span>
        <span style={{ flex: 1 }} />
        <span className="mono" style={{ fontSize: 13, color: "var(--fg)" }}>{used} <span style={{ color: "var(--slate)" }}>/</span> {total} GB</span>
        <span className="mono" style={{ fontSize: 11, color: "var(--slate)" }}>({pct.toFixed(1)}%)</span>
      </div>
      <StackedBar segments={[...segments, { label: "free", value: total - used, tone: "muted" }]} total={total} height={16} />
      {showLegend && (
        <div style={{ display: "flex", gap: 16, marginTop: 10, flexWrap: "wrap" }}>
          {segments.map(seg => (
            <div key={seg.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{
                width: 8, height: 8, borderRadius: 2,
                background: seg.tone === "ok" ? "var(--emerald)" : seg.tone === "cyan" ? "var(--cyan)" : "var(--slate-3)",
                opacity: 0.85,
              }} />
              <span style={{ fontSize: 11, color: "var(--slate)" }}>{seg.label}</span>
              <span className="mono" style={{ fontSize: 11, color: "var(--fg)" }}>{seg.value} GB</span>
            </div>
          ))}
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--slate-3)", opacity: 0.85 }} />
            <span style={{ fontSize: 11, color: "var(--slate)" }}>free</span>
            <span className="mono" style={{ fontSize: 11, color: "var(--fg)" }}>{total - used} GB</span>
          </div>
        </div>
      )}
    </div>
  );
};

// ------------------------------------------------------------- Footer
const Footer = ({ lastRefresh, onRefresh, banner }) => (
  <div className="panel footer-panel" style={{
    display: "flex", alignItems: "center", padding: "10px 16px", gap: 12,
  }}>
    <span className={`pill ${banner.tone}`}>
      <span className={`dot ${banner.tone}`} />
      {banner.text}
    </span>
    <span style={{ flex: 1 }} />
    <span className="mono" style={{ fontSize: 11, color: "var(--slate-2)" }}>
      last refresh <span style={{ color: "var(--fg-dim)" }}>{fmtTime(lastRefresh)}</span>
    </span>
    <button className="btn" onClick={onRefresh}><Icon name="refresh" />Refresh <span className="kbd">R</span></button>
  </div>
);

// ------------------------------------------------------------- Command palette
// Grouped by service. Keyboard shortcut chips. Recent items pinned at top.
const KBD_HINTS = {
  // Stable shortcut hints for common actions. Not actually bound — visual contract.
  "adguard.restart": "⌘⇧A",
  "adguard.open":    "⌘O",
  "k3s.restart":     "⌘⇧K",
  "k3s.events":      "⌘E",
  "kuma.open":       "⌘U",
  "ssh.logs":        "⌘L",
  "smbd.restart":    "⌘⇧S",
  "refresh":         "R",
};

const CommandPalette = ({ open, onClose, onRun, data }) => {
  const [q, setQ] = useState("");
  const [recents, setRecents] = useState([]); // [{id, group, label, action, hint, kind}]
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 30); }, [open]);
  useEffect(() => { if (!open) { setQ(""); setCursor(0); } }, [open]);

  // Build the grouped action catalog.
  const groups = useMemo(() => {
    const g = [];
    data.SERVICES.forEach(s => {
      const items = [];
      items.push({ id: `${s.id}.restart`, label: `Restart`,    hint: s.unit, kbd: KBD_HINTS[`${s.id}.restart`], kind: "danger", icon: "restart", action: { kind: "restart", svc: s } });
      items.push({ id: `${s.id}.logs`,    label: `View logs`,  hint: s.unit, kbd: KBD_HINTS[`${s.id}.logs`],    kind: "info",   icon: "logs",    action: { kind: "logs", svc: s } });
      if (s.ui) items.push({ id: `${s.id}.open`, label: `Open admin UI`, hint: s.ui, kbd: KBD_HINTS[`${s.id}.open`], kind: "info", icon: "external", action: { kind: "open", svc: s } });
      g.push({ id: s.id, title: s.label, glyph: s.glyph, items });
    });
    // k3s extras
    g.find(x => x.id === "k3s")?.items.push({ id: "k3s.events", label: "View events", hint: "kubectl get events -A", kbd: KBD_HINTS["k3s.events"], kind: "info", icon: "logs", action: { kind: "k3s-events" } });
    g.push({
      id: "web-apps", title: "Web Apps", glyph: "external",
      items: (data.WEB_APPS || []).map((app) => ({
        id: `web.${app.id}.open`,
        label: `Open ${app.label}`,
        hint: app.url,
        kind: "info",
        icon: "external",
        action: { kind: "open-url", url: app.url, label: app.label },
      })),
    });
    // Global group
    g.push({
      id: "global", title: "Dashboard", glyph: "command",
      items: [
        { id: "refresh", label: "Refresh all panels", hint: "rerun every probe", kbd: KBD_HINTS["refresh"], kind: "info", icon: "refresh", action: { kind: "refresh" } },
      ],
    });
    return g;
  }, [data]);

  const flatItems = useMemo(() => groups.flatMap(g => g.items.map(i => ({ ...i, group: g.title, groupGlyph: g.glyph, groupId: g.id }))), [groups]);

  const filteredGroups = useMemo(() => {
    if (!q) return groups;
    const Q = q.toLowerCase();
    return groups
      .map(g => ({ ...g, items: g.items.filter(i => i.label.toLowerCase().includes(Q) || i.hint.toLowerCase().includes(Q) || g.title.toLowerCase().includes(Q)) }))
      .filter(g => g.items.length > 0);
  }, [q, groups]);

  const recentItems = useMemo(() => {
    if (q) return [];
    return recents
      .map(id => flatItems.find(i => i.id === id))
      .filter(Boolean)
      .slice(0, 4);
  }, [recents, flatItems, q]);

  // Flat list for cursor navigation
  const cursorList = useMemo(() => {
    const arr = [];
    if (recentItems.length) recentItems.forEach(i => arr.push(i));
    filteredGroups.forEach(g => g.items.forEach(i => arr.push({ ...i, group: g.title, groupId: g.id })));
    return arr;
  }, [recentItems, filteredGroups]);

  const run = (item) => {
    if (!item) return;
    setRecents(prev => [item.id, ...prev.filter(x => x !== item.id)].slice(0, 6));
    onRun(item.action);
    onClose();
  };

  const onKey = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor(c => Math.min(c + 1, cursorList.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setCursor(c => Math.max(0, c - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); run(cursorList[cursor]); }
  };

  if (!open) return null;
  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" style={{ minWidth: 600, maxWidth: 680, marginTop: -100 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--hairline)" }}>
          <Icon name="search" size={16} style={{ color: "var(--slate)" }} />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => { setQ(e.target.value); setCursor(0); }}
            onKeyDown={onKey}
            placeholder="Search services, containers, actions…"
            style={{
              flex: 1, background: "transparent", border: "none", outline: "none",
              color: "var(--fg)", fontFamily: "var(--sans)", fontSize: 14,
            }}
          />
          <span className="kbd">esc</span>
        </div>

        <div style={{ maxHeight: 380, overflow: "auto", padding: 4 }}>
          {recentItems.length > 0 && (
            <PaletteGroup title="Recent" glyph="clock" items={recentItems} cursorStart={0} cursor={cursor} onRun={run} onHover={setCursor} />
          )}
          {filteredGroups.length === 0 && (
            <div style={{ padding: 28, textAlign: "center", color: "var(--slate-2)", fontSize: 12 }}>No matches</div>
          )}
          {(() => {
            let offset = recentItems.length;
            return filteredGroups.map(g => {
              const start = offset;
              offset += g.items.length;
              return (
                <PaletteGroup
                  key={g.id} title={g.title} glyph={g.glyph}
                  items={g.items.map(i => ({ ...i, group: g.title, groupId: g.id }))}
                  cursorStart={start} cursor={cursor} onRun={run} onHover={setCursor}
                />
              );
            });
          })()}
        </div>

        <div style={{
          padding: "8px 14px", borderTop: "1px solid var(--hairline)",
          fontSize: 10, color: "var(--slate-2)", display: "flex", gap: 14, alignItems: "center",
        }}>
          <span><span className="kbd">↑↓</span> navigate</span>
          <span><span className="kbd">↵</span> run</span>
          <span><span className="kbd">esc</span> close</span>
          <span style={{ flex: 1 }} />
          <span style={{ color: "var(--slate-3)" }}>{cursorList.length} actions</span>
        </div>
      </div>
    </div>
  );
};

const PaletteGroup = ({ title, glyph, items, cursorStart, cursor, onRun, onHover }) => (
  <div style={{ marginTop: 4 }}>
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "8px 10px 4px",
      fontSize: 10, color: "var(--slate)",
      textTransform: "uppercase", letterSpacing: "0.10em",
    }}>
      <Icon name={glyph} size={11} />
      {title}
    </div>
    {items.map((it, i) => {
      const idx = cursorStart + i;
      const active = idx === cursor;
      return (
        <button key={it.id}
          onClick={() => onRun(it)}
          onMouseEnter={() => onHover(idx)}
          style={{
            width: "100%", textAlign: "left",
            border: "none",
            background: active ? "rgba(34,211,238,0.08)" : "transparent",
            color: "var(--fg)", padding: "8px 10px",
            borderRadius: 7, display: "flex", alignItems: "center", gap: 10,
            cursor: "pointer", fontFamily: "var(--sans)",
            borderLeft: active ? "2px solid var(--cyan)" : "2px solid transparent",
          }}>
          <span className={`chip-btn ${it.kind === "danger" ? "warn" : ""}`} style={{
            // Inside palette these are non-interactive — just visual chips
            cursor: "default", pointerEvents: "none",
          }}>
            <Icon name={it.icon} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, color: "var(--fg)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.label}</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.hint}</div>
          </div>
          {it.kbd && <span className="kbd" style={{ fontSize: 10 }}>{it.kbd}</span>}
        </button>
      );
    })}
  </div>
);

// ------------------------------------------------------------- Confirm modal
const ConfirmModal = ({ open, payload, onCancel, onConfirm }) => {
  if (!open || !payload) return null;
  const { title, body, danger, confirmLabel } = payload;
  return (
    <div className="scrim" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ padding: "18px 20px", borderBottom: "1px solid var(--hairline)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span className={`chip ${danger ? "warn" : "cyan"}`}><Icon name={danger ? "alert" : "info"} /></span>
            <div style={{ fontSize: 14, fontWeight: 500, color: "var(--fg)" }}>{title}</div>
          </div>
        </div>
        <div style={{ padding: "18px 20px", color: "var(--fg-dim)", fontSize: 13 }}>{body}</div>
        <div style={{ padding: "12px 16px", borderTop: "1px solid var(--hairline)",
          display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
          <button className={`btn ${danger ? "danger" : ""}`}
            style={{ borderColor: danger ? "var(--amber)" : "var(--cyan)", color: danger ? "var(--amber)" : "var(--cyan)" }}
            onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- KPI detail modal
const KPIDetail = ({ kpi, onClose }) => {
  if (!kpi) return null;
  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" style={{ minWidth: 720, maxWidth: 880 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ padding: "16px 20px", borderBottom: "1px solid var(--hairline)",
          display: "flex", alignItems: "center", gap: 12 }}>
          <span className="chip ok"><Icon name={kpi.glyph} /></span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>{kpi.label}</div>
            <div className="mono" style={{ fontSize: 11, color: "var(--slate-2)" }}>
              current <span style={{ color: "var(--fg)" }}>{kpi.value}{kpi.suffix}</span>
              {kpi.sub && <> · {kpi.sub}</>}
            </div>
          </div>
          <button className="chip-btn" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div style={{ padding: "16px 20px" }}>
          <TimeSeries
            seriesList={[{ values: kpi.history, color: "var(--cyan)" }]}
            height={260}
          />
        </div>
        <div style={{ padding: "12px 20px 16px", display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 16 }}>
          {[
            ["min",  Math.min(...kpi.history).toFixed(1)],
            ["max",  Math.max(...kpi.history).toFixed(1)],
            ["avg",  (kpi.history.reduce((a,b) => a+b,0)/kpi.history.length).toFixed(1)],
            ["p95",  [...kpi.history].sort((a,b)=>a-b)[Math.floor(kpi.history.length * 0.95)].toFixed(1)],
          ].map(([k,v]) => (
            <div key={k}>
              <div style={{ fontSize: 10, color: "var(--slate)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{k}</div>
              <div className="mono" style={{ fontSize: 18, color: "var(--fg)", marginTop: 2 }}>{v}{kpi.suffix}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- Logs modal
const LogsModal = ({ view, onClose }) => {
  if (!view) return null;
  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" style={{ minWidth: 760, maxWidth: "86vw" }} onClick={(e) => e.stopPropagation()}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--hairline)", display: "flex", alignItems: "center", gap: 10 }}>
          <span className={`chip ${view.error ? "fail" : "cyan"}`}><Icon name={view.error ? "alert" : "logs"} /></span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>{view.title}</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>{view.hint}</div>
          </div>
          <button className="chip-btn" onClick={onClose}><Icon name="x" /></button>
        </div>
        <pre className="mono" style={{
          margin: 0,
          padding: 16,
          minHeight: 280,
          maxHeight: "64vh",
          overflow: "auto",
          whiteSpace: "pre-wrap",
          color: view.error ? "var(--rose)" : "var(--fg-dim)",
          background: "rgba(3,6,15,0.28)",
          fontSize: 11,
          lineHeight: 1.45,
        }}>{view.text || "Loading..."}</pre>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- Ops panel
const OpsPanel = ({ onAction, onLogs, jobs }) => {
  const workloads = MOCK.K3S.workloads || [];
  const allowedWorkloads = workloads.filter((w) => w.rolloutAllowed);
  return (
    <div className="panel">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="chip warn"><Icon name="command" /></span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>Ops controls</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--slate-2)" }}>LAN direct · server allowlist enforced</div>
          </div>
        </div>
        <span className="pill warn"><Icon name="alert" size={11} /> direct controls</span>
      </div>
      <div className="ops-grid" style={{ padding: "var(--s-5)", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>AdGuard</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button className="btn" onClick={() => window.open("http://192.168.0.101:8080", "_blank", "noopener,noreferrer")}><Icon name="external" />Open admin</button>
            <button className="btn" onClick={() => onAction({ type: "adguard", action: "refresh_filters" })}><Icon name="refresh" />Refresh filters</button>
            <button className="btn" onClick={() => onAction({ type: "adguard", action: "clear_cache" })}><Icon name="x" />Clear DNS cache</button>
          </div>
        </div>
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>k3s rollouts</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 126, overflow: "auto" }}>
            {allowedWorkloads.length === 0 && <span style={{ color: "var(--slate-2)", fontSize: 11 }}>No rollout-safe workloads discovered</span>}
            {allowedWorkloads.map((w) => (
              <button key={`${w.namespace}/${w.kind}/${w.name}`} className="btn" style={{ justifyContent: "flex-start" }}
                onClick={() => onAction({ type: "k3s", action: "rollout_restart", namespace: w.namespace, kind: w.kind, name: w.name })}>
                <Icon name="restart" />
                <span className="mono">{w.namespace}/{w.name}</span>
              </button>
            ))}
          </div>
        </div>
        <div>
          <div className="panel-title" style={{ marginBottom: 10 }}>Recent action jobs</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 126, overflow: "auto" }}>
            {jobs.length === 0 && <span style={{ color: "var(--slate-2)", fontSize: 11 }}>No actions run this session</span>}
            {jobs.map((j) => (
              <div key={j.id} style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 8, alignItems: "center", fontSize: 11 }}>
                <span className={`dot ${j.status === "succeeded" ? "ok" : j.status === "failed" ? "fail" : "warn"}`} />
                <span className="mono" style={{ color: "var(--fg-dim)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.label}</span>
                {j.output && <button className="chip-btn" title="View output" onClick={() => onLogs(j)}><Icon name="logs" /></button>}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

// ------------------------------------------------------------- Mobile shell
const MobileHeader = ({ host, feed, banner, activeSection, alertCount, onRefresh, onOpenPalette, onLogout }) => {
  const now = useNow();
  const active = MOBILE_SECTIONS.find((section) => section.id === activeSection) || MOBILE_SECTIONS[0];
  const feedTone = feed?.connected && !feed?.stale ? "ok" : feed?.connected ? "warn" : "fail";
  return (
    <header className="mobile-header">
      <div className="mobile-header-main">
        <div className="mobile-brand">
          <div className="mobile-mark"><span className="mono">π</span></div>
          <div style={{ minWidth: 0 }}>
            <div className="mobile-title-row">
              <span className={`dot ${feedTone}`} />
              <span className="mono mobile-host">Cabrera Network</span>
              <span className="mobile-section-label">{active.label}</span>
            </div>
            <div className="mono mobile-subtitle">
              {host?.ip || "192.168.0.101"} · {fmtTime(now)} · {host?.uptime || "uptime unknown"}
            </div>
          </div>
        </div>
        <div className="mobile-header-actions">
          <button className="chip-btn" title="Refresh" onClick={onRefresh}>
            <Icon name="refresh" />
          </button>
          <button className="chip-btn" title="Search actions" onClick={onOpenPalette}>
            <Icon name="search" />
          </button>
          <button className="chip-btn" title="Logout" onClick={onLogout}>
            <Icon name="x" />
          </button>
        </div>
      </div>
      <div className="mobile-status-strip">
        <span className={`pill ${banner.tone}`}><span className={`dot ${banner.tone}`} />{banner.text}</span>
        {alertCount > 0 && <span className="pill warn"><Icon name="bell" size={11} />{alertCount} alerts</span>}
      </div>
    </header>
  );
};

const MobileBottomNav = ({ activeSection, onChange }) => (
  <nav className="mobile-bottom-nav" aria-label="Dashboard sections">
    {MOBILE_SECTIONS.map((section) => (
      <button
        key={section.id}
        type="button"
        className={activeSection === section.id ? "active" : ""}
        aria-current={activeSection === section.id ? "page" : undefined}
        onClick={() => onChange(section.id)}
      >
        <Icon name={section.icon} />
        <span>{section.label}</span>
      </button>
    ))}
  </nav>
);

const MobileStatusPanel = ({ host, feed, banner, lastRefresh, onRefresh }) => {
  const feedTone = feed?.connected && !feed?.stale ? "ok" : feed?.connected ? "warn" : "fail";
  return (
    <div className="panel mobile-status-panel">
      <div className="mobile-status-primary">
        <span className={`chip ${feedTone}`}><Icon name={feedTone === "ok" ? "check" : "info"} /></span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500 }}>{banner.text}</div>
          <div className="mono" style={{ color: "var(--slate-2)", fontSize: 11 }}>
            {host?.os || "Raspberry Pi OS"} · load {(host?.loadAvg || []).slice(0, 3).join(" / ") || "n/a"}
          </div>
        </div>
      </div>
      <div className="mobile-status-meta">
        <span className="pill"><Icon name="clock" size={11} />refresh {fmtTime(lastRefresh)}</span>
        <span className="pill cyan"><Icon name="activity" size={11} />temp {host?.tempC ?? "-"}°C</span>
        <button className="btn" onClick={onRefresh}><Icon name="refresh" />Refresh</button>
      </div>
    </div>
  );
};

const MobileServiceAlerts = ({ services, onAction }) => {
  const alerts = services.filter((svc) => svc.status !== "ok");
  const rows = alerts.length ? alerts : services.slice(0, 4);
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="panel-title">Service pulse</div>
        <span className={`pill ${alerts.length ? "warn" : "ok"}`}>
          <span className={`dot ${alerts.length ? "warn" : "ok"}`} />
          {alerts.length ? `${alerts.length} need attention` : "all green"}
        </span>
      </div>
      <div className="mobile-alert-list">
        {rows.map((svc) => (
          <div key={svc.id} className="mobile-alert-row">
            <span className={`chip ${svc.status === "ok" ? "ok" : svc.status === "warn" ? "warn" : "fail"}`}>
              <Icon name={svc.glyph} />
            </span>
            <div style={{ minWidth: 0 }}>
              <div className="mobile-row-title">{svc.label}</div>
              <div className="mono mobile-row-sub">{svc.unit} · :{svc.port}</div>
            </div>
            <button className="chip-btn" title="Logs" onClick={() => onAction("logs", svc)}>
              <Icon name="logs" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};

const MobileAdGuardSummary = () => {
  const a = MOCK.ADGUARD;
  return (
    <div className="panel mobile-adguard-summary">
      <div className="panel-head">
        <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <span className="chip ok"><Icon name="brandShield" /></span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 500 }}>AdGuard Home</div>
            <div className="mono mobile-row-sub">{a.upstream}</div>
          </div>
        </div>
        <span className="pill ok"><span className="dot ok" />active</span>
      </div>
      <div className="mobile-stat-grid">
        <StatLine label="Queries" value={a.queries.toLocaleString()} />
        <StatLine label="Blocked" value={a.blocked.toLocaleString()} tone="rose" />
        <StatLine label="Block rate" value={`${a.blockRatio.toFixed(1)}%`} tone="rose" />
      </div>
    </div>
  );
};

const MOBILE_CHARTS = [
  {
    id: "cpu",
    title: "CPU + Load",
    legend: "cpu % · load×10",
    seriesList: () => [
      { values: MOCK.HISTORY.cpu, color: "var(--cyan)" },
      { values: MOCK.HISTORY.loadAvg.map(v => v * 10), color: "var(--emerald)", area: false },
    ],
  },
  {
    id: "network",
    title: "Network",
    legend: "in · out kbps",
    seriesList: () => [
      { values: MOCK.HISTORY.netIn, color: "var(--cyan)" },
      { values: MOCK.HISTORY.netOut, color: "var(--amber)" },
    ],
  },
  {
    id: "dns",
    title: "DNS",
    legend: "total · blocked",
    seriesList: () => [{ values: MOCK.HISTORY.dnsTotal, color: "var(--cyan)" }],
    bars: () => ({ values: MOCK.HISTORY.dnsBlocked, color: "var(--rose)" }),
  },
  {
    id: "disk",
    title: "Disk I/O",
    legend: "read · write MB/s",
    seriesList: () => [
      { values: MOCK.HISTORY.diskRead, color: "var(--emerald)" },
      { values: MOCK.HISTORY.diskWrite, color: "var(--amber)" },
    ],
  },
];

const MobileMiniCharts = () => {
  const [expanded, setExpanded] = useState("cpu");
  return (
    <div className="mobile-chart-grid">
      {MOBILE_CHARTS.map((chart) => {
        const isExpanded = expanded === chart.id;
        return (
          <button
            key={chart.id}
            type="button"
            className={`mobile-chart-card ${isExpanded ? "expanded" : ""}`}
            onClick={() => setExpanded(isExpanded ? null : chart.id)}
          >
            <div className="mobile-chart-head">
              <span>{chart.title}</span>
              <span className="mono">{chart.legend}</span>
            </div>
            <TimeSeries
              seriesList={chart.seriesList()}
              bars={chart.bars?.()}
              height={isExpanded ? 150 : 74}
            />
          </button>
        );
      })}
    </div>
  );
};

const mobileBucketIdFor = (client) => {
  const group = String(client.groupId || "").toLowerCase();
  if (["wired", "basement", "loft", "wifi-unknown", "unknown"].includes(group)) return group;
  const apId = String(client.apId || "").toLowerCase();
  if (["basement", "loft"].includes(apId)) return apId;
  const link = String(client.linkType || client.interface || "").toLowerCase();
  if (link === "wired") return "wired";
  if (isWifiLink(link)) return "wifi-unknown";
  return "unknown";
};

const buildMobileTopologyBuckets = (topology = {}, host = {}) => {
  const router = topology.router || { name: "Archer BE400", ip: "192.168.0.1" };
  const aps = Array.isArray(topology.aps) ? topology.aps : [];
  const clients = Array.isArray(topology.clients) ? topology.clients : [];
  const hasHostClient = clients.some((client) => client.ip && host?.ip && client.ip === host.ip);
  const hostRow = {
    key: "host-pi4",
    displayName: host?.name || "pi4",
    sourceName: host?.name || "pi4",
    ip: host?.ip || "192.168.0.101",
    linkType: "wired",
    glyph: "cpu",
    online: true,
    confidence: "high",
    sourceText: "dashboard host",
  };
  const byBucket = {
    wired: hasHostClient ? [] : [hostRow],
    basement: [],
    loft: [],
    "wifi-unknown": [],
    unknown: [],
  };
  clients.forEach((client) => {
    const bucket = mobileBucketIdFor(client);
    byBucket[bucket]?.push(client);
  });
  Object.keys(byBucket).forEach((key) => {
    byBucket[key] = sortTopologyRows(byBucket[key]);
  });
  const routerDevices = sortTopologyRows(aps.map((ap) => ({
    ...ap,
    displayName: ap.displayName || ap.name || ap.ip || "Access point",
    glyph: "mesh",
    isAp: true,
  })));
  const bucketStats = (rows) => ({
    count: rows.length,
    online: rows.filter((row) => row.online).length,
    kbps: rows.reduce((sum, row) => sum + asNumber(row.rxKbps) + asNumber(row.txKbps), 0),
  });
  return [
    {
      id: "main",
      label: router.name || "Archer BE400",
      sub: router.ip || "192.168.0.1",
      glyph: "router",
      tone: "cyan",
      devices: routerDevices,
      empty: "No APs reported yet",
      statLabel: "infrastructure",
    },
    {
      id: "basement",
      label: apLabel("basement", aps),
      sub: aps.find((ap) => ap.apId === "basement" || ap.id === "basement")?.ip || "Basement AP",
      glyph: "network",
      tone: "ok",
      devices: byBucket.basement,
      empty: "No Basement clients observed",
      statLabel: "clients",
    },
    {
      id: "loft",
      label: apLabel("loft", aps),
      sub: aps.find((ap) => ap.apId === "loft" || ap.id === "loft")?.ip || "Loft AP",
      glyph: "network",
      tone: "ok",
      devices: byBucket.loft,
      empty: "No Loft clients observed",
      statLabel: "clients",
    },
    {
      id: "wired",
      label: "Wired LAN",
      sub: "Router switch / Ethernet",
      glyph: "network",
      tone: "cyan",
      devices: byBucket.wired,
      empty: "No wired clients observed",
      statLabel: "devices",
    },
    {
      id: "wifi-unknown",
      label: "Wi-Fi AP unknown",
      sub: "Router has band, no AP",
      glyph: "network",
      tone: "warn",
      devices: byBucket["wifi-unknown"],
      empty: "No AP-unknown Wi-Fi clients",
      statLabel: "devices",
    },
    {
      id: "unknown",
      label: "Link unknown",
      sub: "LAN observed only",
      glyph: "question",
      tone: "warn",
      devices: byBucket.unknown,
      empty: "No unknown-link clients",
      statLabel: "devices",
    },
  ].map((bucket) => ({ ...bucket, stats: bucketStats(bucket.devices) }));
};

const MobileTopologyDeviceLine = ({ device }) => {
  const link = device.linkType || "unknown";
  const name = device.displayName || device.name || device.ip || "unknown";
  return (
    <div className="mobile-device-line">
      <span className={`chip ${device.isAp ? "ok" : linkTone(link)}`}>
        <Icon name={device.isAp ? "network" : iconForDeviceGlyph(device.glyph)} />
      </span>
      <div style={{ minWidth: 0 }}>
        <div className="mobile-row-title">{name}</div>
        <div className="mono mobile-row-sub">
          {device.ip || device.mac || "no address"} · {device.sourceText || device.confidence || "observed"}
        </div>
      </div>
      <span className={`pill ${device.isAp ? "cyan" : linkTone(link)}`}>{device.isAp ? "AP" : linkLabel(link)}</span>
    </div>
  );
};

const MobileTopologyBucket = ({ bucket, compact = false }) => {
  const visible = bucket.devices.slice(0, compact ? 3 : 8);
  const hidden = Math.max(0, bucket.devices.length - visible.length);
  return (
    <div className="panel mobile-topology-bucket">
      <div className="mobile-bucket-head">
        <span className={`chip ${bucket.tone}`}><Icon name={bucket.glyph} /></span>
        <div style={{ minWidth: 0 }}>
          <div className="mobile-row-title">{bucket.label}</div>
          <div className="mono mobile-row-sub">{bucket.sub}</div>
        </div>
        <div className="mobile-bucket-metrics">
          <span className="pill"><span className="mono">{bucket.stats.online}/{bucket.stats.count}</span> {bucket.statLabel}</span>
          {bucket.stats.kbps > 0 && <span className="pill cyan">{fmtKbps(bucket.stats.kbps)}</span>}
        </div>
      </div>
      <div className="mobile-device-list">
        {visible.length === 0 ? (
          <div className="mobile-empty">{bucket.empty}</div>
        ) : visible.map((device) => (
          <MobileTopologyDeviceLine key={`${device.key || device.mac || device.ip || device.displayName}`} device={device} />
        ))}
        {hidden > 0 && <div className="mobile-more-row">+{hidden} more in the device table</div>}
      </div>
    </div>
  );
};

const MobileTopologySummary = ({ onOpenNetwork }) => {
  const topology = MOCK.TOPOLOGY || {};
  const buckets = buildMobileTopologyBuckets(topology, MOCK.HOST || {});
  const counts = topology.counts || {};
  const routerCollector = topology.routerCollector || {};
  const collectorTone = routerCollectorTone(routerCollector);
  return (
    <div className="panel mobile-topology-summary">
      <div className="panel-head">
        <div className="panel-title">Network topology</div>
        <button className="btn ghost" onClick={onOpenNetwork}>Open <Icon name="chevron" /></button>
      </div>
      <div className="mobile-summary-counts">
        <span className="pill cyan"><Icon name="activity" size={11} /> {fmtKbps(topology.aggregateKbps)}</span>
        <span className="pill"><span className="mono">{counts.apsOnline || 0}/{counts.aps || 0}</span> APs</span>
        <span className="pill ok"><span className="mono">{counts.wifi || 0}</span> Wi-Fi</span>
        <span className="pill"><span className="mono">{counts.wired || 0}</span> wired</span>
        <span className={`pill ${collectorTone}`} title={routerCollector.lastError || routerCollector.state || ""}>
          {routerCollectorLabel(routerCollector)}
        </span>
      </div>
      <div className="mobile-topology-mini-grid">
        {buckets.slice(0, 4).map((bucket) => <MobileTopologyBucket key={bucket.id} bucket={bucket} compact />)}
      </div>
    </div>
  );
};

const MobileTopologyPanel = ({ onSaved, onToast }) => {
  const topology = MOCK.TOPOLOGY || {};
  const buckets = buildMobileTopologyBuckets(topology, MOCK.HOST || {});
  const aps = Array.isArray(topology.aps) ? topology.aps : [];
  const clients = Array.isArray(topology.clients) ? topology.clients : [];
  const counts = topology.counts || {};
  const routerCollector = topology.routerCollector || {};
  const collectorTone = routerCollectorTone(routerCollector);
  const devices = [...aps.map((ap) => ({ ...ap, isAp: true, groupId: ap.apId || "ap" })), ...clients].slice(0, 72);
  return (
    <div className="mobile-tab-stack">
      <div className="panel">
        <div className="panel-head mobile-panel-head-wrap">
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            <span className="chip cyan"><Icon name="network" /></span>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 500 }}>Network topology</div>
              <div className="mono mobile-row-sub">{counts.online || 0} online · {counts.known || counts.total || clients.length} known</div>
            </div>
          </div>
          <span className={`pill ${collectorTone}`}>{routerCollectorLabel(routerCollector)}</span>
        </div>
        <div className="mobile-summary-counts">
          <span className="pill cyan"><Icon name="activity" size={11} /> {fmtKbps(topology.aggregateKbps)}</span>
          <span className="pill"><span className="mono">{counts.apsOnline || 0}/{counts.aps || aps.length}</span> APs</span>
          <span className="pill"><span className="mono">{counts.wired || 0}</span> wired</span>
          <span className="pill ok"><span className="mono">{counts.wifi || 0}</span> Wi-Fi</span>
          <span className="pill warn"><span className="mono">{counts.unknown || 0}</span> unknown</span>
        </div>
      </div>
      <div className="mobile-topology-buckets">
        {buckets.map((bucket) => <MobileTopologyBucket key={bucket.id} bucket={bucket} />)}
      </div>
      <div className="panel">
        <div className="panel-head">
          <div className="panel-title">Device aliases</div>
          <span className="pill">LAN edits</span>
        </div>
        <div style={{ padding: 10 }}>
          <TopologyDeviceTable devices={devices} aps={aps} onSaved={onSaved} onToast={onToast} />
        </div>
      </div>
    </div>
  );
};

const MobileOverview = ({ feed, banner, alertCount, tickedKpi, setKpiDetail, onRefresh, onServiceAction, onOpenNetwork }) => (
  <div className="mobile-tab-stack">
    <MobileStatusPanel host={MOCK.HOST} feed={feed} banner={banner} lastRefresh={feed.lastRefresh} onRefresh={onRefresh} />
    <KPIStrip kpis={MOCK.KPIS} tickedId={tickedKpi} onSelect={setKpiDetail} />
    <WebAppsPanel apps={MOCK.WEB_APPS || []} />
    <MobileServiceAlerts services={MOCK.SERVICES} onAction={onServiceAction} />
    <MobileTopologySummary onOpenNetwork={onOpenNetwork} />
    <MobileAdGuardSummary />
    <MobileMiniCharts />
  </div>
);

const MobileServices = ({ onServiceAction }) => (
  <div className="mobile-tab-stack">
    <ServicesPanel services={MOCK.SERVICES} onAction={onServiceAction} />
    <K3sPanel />
  </div>
);

const MobileLogsPanel = ({ services, jobs, onOpenLogs, onJobLogs }) => (
  <div className="mobile-tab-stack">
    <div className="panel">
      <div className="panel-head">
        <div className="panel-title">System logs</div>
        <span className="pill">{services.length} units</span>
      </div>
      <div className="mobile-log-list">
        {services.map((svc) => (
          <button
            key={svc.id}
            type="button"
            className="mobile-log-row"
            onClick={() => onOpenLogs({ sourceType: "systemd", id: svc.unit, lines: 180 }, `${svc.label} logs`, svc.unit)}
          >
            <span className={`chip ${svc.status === "ok" ? "ok" : svc.status === "warn" ? "warn" : "fail"}`}><Icon name={svc.glyph} /></span>
            <span>
              <span className="mobile-row-title">{svc.label}</span>
              <span className="mono mobile-row-sub">{svc.unit}</span>
            </span>
            <Icon name="logs" />
          </button>
        ))}
        <button
          type="button"
          className="mobile-log-row"
          onClick={() => onOpenLogs({ sourceType: "k3s-events", id: "events", lines: 120 }, "k3s events", "kubectl get events -A")}
        >
          <span className="chip ok"><Icon name="brandCubes" /></span>
          <span>
            <span className="mobile-row-title">k3s events</span>
            <span className="mono mobile-row-sub">cluster event stream</span>
          </span>
          <Icon name="logs" />
        </button>
      </div>
    </div>
    <div className="panel">
      <div className="panel-head">
        <div className="panel-title">Action outputs</div>
        <span className="pill">{jobs.length} recent</span>
      </div>
      <div className="mobile-log-list">
        {jobs.length === 0 && <div className="mobile-empty">No action output in this session</div>}
        {jobs.map((job) => (
          <button
            key={job.id}
            type="button"
            className="mobile-log-row"
            disabled={!job.output && !job.error}
            onClick={() => onJobLogs(job)}
          >
            <span className={`dot ${job.status === "succeeded" ? "ok" : job.status === "failed" ? "fail" : "warn"}`} />
            <span>
              <span className="mobile-row-title">{job.label}</span>
              <span className="mono mobile-row-sub">{job.status} · {job.id}</span>
            </span>
            <Icon name="logs" />
          </button>
        ))}
      </div>
    </div>
  </div>
);

const MobileDashboard = ({
  activeSection,
  onSectionChange,
  feed,
  banner,
  alertCount,
  tickedKpi,
  setKpiDetail,
  onRefresh,
  onOpenPalette,
  onServiceAction,
  onOpsAction,
  onJobLogs,
  onOpenLogs,
  onLogout,
  jobs,
  toasts,
}) => (
  <>
    <MobileHeader
      host={MOCK.HOST}
      feed={feed}
      banner={banner}
      activeSection={activeSection}
      alertCount={alertCount}
      onRefresh={onRefresh}
      onOpenPalette={onOpenPalette}
      onLogout={onLogout}
    />
    <main className="mobile-stage">
      {activeSection === "overview" && (
        <MobileOverview
          feed={feed}
          banner={banner}
          alertCount={alertCount}
          tickedKpi={tickedKpi}
          setKpiDetail={setKpiDetail}
          onRefresh={onRefresh}
          onServiceAction={onServiceAction}
          onOpenNetwork={() => onSectionChange("network")}
        />
      )}
      {activeSection === "network" && (
        <MobileTopologyPanel onSaved={feed.refresh} onToast={toasts.push} />
      )}
      {activeSection === "services" && (
        <MobileServices onServiceAction={onServiceAction} />
      )}
      {activeSection === "ops" && (
        <div className="mobile-tab-stack mobile-ops-stack">
          <OpsPanel jobs={jobs} onAction={onOpsAction} onLogs={onJobLogs} />
        </div>
      )}
      {activeSection === "logs" && (
        <MobileLogsPanel
          services={MOCK.SERVICES}
          jobs={jobs}
          onOpenLogs={onOpenLogs}
          onJobLogs={onJobLogs}
        />
      )}
    </main>
    <MobileBottomNav activeSection={activeSection} onChange={onSectionChange} />
  </>
);

// ------------------------------------------------------------- App
function DashboardApp({ onLogout }) {
  const feed = useDashboardFeed();
  MOCK = feed.data || DEFAULT_DASHBOARD;
  const [density, setDensity] = useState("compact");
  const [theme, setTheme] = useState("dark");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [legendOpen, setLegendOpen] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [kpiDetail, setKpiDetail] = useState(null);
  const [logView, setLogView] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [tickedKpi, setTickedKpi] = useState(null);
  const [activeMobileSection, setActiveMobileSection] = useState("overview");
  const isMobile = useMediaQuery("(max-width: 820px)");
  const toasts = useToasts();
  const alertCount = (MOCK.SERVICES || []).filter((svc) => svc.status !== "ok").length;
  const banner = feed.connected
    ? feed.stale
      ? { tone: "warn", text: "Live feed stale" }
      : { tone: alertCount ? "warn" : "ok", text: alertCount ? `${alertCount} service alerts` : "Live data connected" }
    : { tone: "fail", text: feed.error ? "API offline" : "Connecting to API" };

  // Apply theme/density on root
  useEffect(() => { document.documentElement.dataset.density = density; }, [density]);
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);

  // ⌘K + R + Esc shortcuts
  useEffect(() => {
    const fn = (e) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault(); setPaletteOpen(p => !p);
      } else if (e.key === "Escape") {
        setPaletteOpen(false); setConfirm(null); setKpiDetail(null); setLegendOpen(false);
      } else if (e.key === "r" && !e.metaKey && !e.ctrlKey && !paletteOpen && !confirm && !kpiDetail) {
        if (document.activeElement?.tagName !== "INPUT") doRefresh();
      }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [paletteOpen, confirm, kpiDetail]);

  // Live ticker — rotate which KPI is "ticked" each second
  useEffect(() => {
    const t = setInterval(() => {
      setTickedKpi((current) => {
        const ids = (MOCK.KPIS || []).map((k) => k.id);
        if (!ids.length) return null;
        const idx = Math.max(0, ids.indexOf(current));
        return ids[(idx + 1) % ids.length];
      });
    }, 1100);
    return () => clearInterval(t);
  }, []);

  const pollJob = useCallback((job) => {
    let attempts = 0;
    const tick = async () => {
      attempts += 1;
      try {
        const next = await getActionJob(job.id);
        setJobs((prev) => prev.map((j) => (j.id === next.id ? next : j)));
        if (next.status === "succeeded") {
          toasts.push(`${next.label} completed`, "ok");
          feed.refresh().catch(() => {});
          return;
        }
        if (next.status === "failed") {
          toasts.push(`${next.label} failed`, "rose");
          return;
        }
      } catch (err) {
        toasts.push(`Action status check failed: ${err.message}`, "rose");
        return;
      }
      if (attempts < 30) setTimeout(tick, 1000);
    };
    setTimeout(tick, 700);
  }, [feed, toasts]);

  const runOpsAction = useCallback(async (payload, tone = "amber") => {
    try {
      const job = await postAction(payload);
      setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)].slice(0, 8));
      toasts.push(`${job.label} started`, tone);
      pollJob(job);
    } catch (err) {
      toasts.push(err.message || "Action failed to start", "rose");
    }
  }, [pollJob, toasts]);

  const openLogs = useCallback(async (request, title, hint) => {
    setLogView({ title, hint, text: "" });
    try {
      const res = await fetchLogs(request);
      setLogView({ title: res.title || title, hint: res.hint || hint, text: res.text || "(no log output)" });
    } catch (err) {
      setLogView({ title, hint, text: err.message, error: true });
    }
  }, []);

  const doRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await feed.refresh();
      toasts.push("Refreshed all panels", "cyan");
    } catch (err) {
      toasts.push(`Refresh failed: ${err.message}`, "rose");
    } finally {
      setTimeout(() => setRefreshing(false), 720);
    }
  }, [feed, toasts]);

  const handleServiceAction = (kind, svc) => {
    if (kind === "restart") {
      setConfirm({
        title: `Restart ${svc.label}?`,
        body: <>This will run <span className="mono" style={{ color: "var(--cyan)" }}>systemctl restart {svc.unit}</span>. Service will be unavailable for a few seconds.</>,
        danger: true,
        confirmLabel: "Restart",
        onConfirm: () => runOpsAction({ type: "systemd", action: "restart", unit: svc.unit }),
      });
    } else if (kind === "logs") {
      openLogs({ sourceType: "systemd", id: svc.unit, lines: 180 }, `${svc.label} logs`, svc.unit);
    } else if (kind === "open") {
      toasts.push(`Opening ${svc.ui}…`, "cyan");
      window.open(svc.ui, "_blank", "noopener,noreferrer");
    }
  };

  const handlePaletteAction = (action) => {
    if (action.kind === "restart") handleServiceAction("restart", action.svc);
    else if (action.kind === "logs") handleServiceAction("logs", action.svc);
    else if (action.kind === "open") handleServiceAction("open", action.svc);
    else if (action.kind === "open-url")          { toasts.push(`Opening ${action.label}...`, "cyan"); window.open(action.url, "_blank", "noopener,noreferrer"); }
    else if (action.kind === "k3s-events")        openLogs({ sourceType: "k3s-events", id: "events", lines: 120 }, "k3s events", "kubectl get events -A");
    else if (action.kind === "refresh")           doRefresh();
  };

  const handleOpsAction = (payload) => {
    const label = payload.type === "adguard"
      ? payload.action.replace("_", " ")
      : payload.type === "k3s"
        ? `rollout restart ${payload.namespace}/${payload.name}`
        : "ops action";
    setConfirm({
      title: `Run ${label}?`,
      body: <>This request will be executed by the Pi sidecar through its allowlist.</>,
      danger: true,
      confirmLabel: "Run",
      onConfirm: () => runOpsAction(payload),
    });
  };

  const handleJobLogs = (job) => {
    setLogView({
      title: `${job.label} output`,
      hint: job.id,
      text: job.output || job.error || "(no output)",
      error: job.status === "failed",
    });
  };

  return (
    <div className={refreshing ? "refreshing" : ""}>
      {isMobile ? (
        <MobileDashboard
          activeSection={activeMobileSection}
          onSectionChange={setActiveMobileSection}
          feed={feed}
          banner={banner}
          alertCount={alertCount}
          tickedKpi={tickedKpi}
          setKpiDetail={setKpiDetail}
          onRefresh={doRefresh}
          onOpenPalette={() => setPaletteOpen(true)}
          onServiceAction={handleServiceAction}
          onOpsAction={handleOpsAction}
          onJobLogs={handleJobLogs}
          onOpenLogs={openLogs}
          onLogout={onLogout}
          jobs={jobs}
          toasts={toasts}
        />
      ) : (
        <>
          <TopBar
            onOpenPalette={() => setPaletteOpen(true)}
            onOpenLegend={() => setLegendOpen(o => !o)}
            onToggleDensity={(d) => setDensity(d)}
            onToggleTheme={() => setTheme(t => t === "dark" ? "darker" : "dark")}
            onLogout={onLogout}
            density={density}
            theme={theme}
            alertCount={alertCount}
            refreshing={refreshing}
            host={MOCK.HOST}
            feed={feed}
          />

          <div className="stage dashboard-stage" style={{ paddingTop: 10, paddingBottom: 16, display: "flex", flexDirection: "column", gap: 8 }}>

            {/* KPI strip */}
            <KPIStrip kpis={MOCK.KPIS} tickedId={tickedKpi} onSelect={setKpiDetail} />

            {/* AdGuard HERO — full-width band, top of the main content */}
            <AdGuardHero />

            {/* Main row: 2x2 charts left, services right */}
            <div className="dashboard-main-row" style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 8 }}>
              <ChartsGrid />
              <ServicesPanel services={MOCK.SERVICES} onAction={handleServiceAction} />
            </div>

            <WebAppsPanel apps={MOCK.WEB_APPS || []} />

            {/* k3s panel */}
            <K3sPanel />

            {/* Direct ops controls */}
            <OpsPanel
              jobs={jobs}
              onAction={handleOpsAction}
              onLogs={handleJobLogs}
            />

            {/* Network topology — replaces storage */}
            <TopologyPanel onSaved={feed.refresh} onToast={toasts.push} />

            {/* Footer */}
            <Footer
              lastRefresh={feed.lastRefresh}
              onRefresh={doRefresh}
              banner={banner}
            />
          </div>
        </>
      )}

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onRun={handlePaletteAction} data={MOCK} />
      <IconLegend open={legendOpen} onClose={() => setLegendOpen(false)} />
      <ConfirmModal open={!!confirm} payload={confirm}
        onCancel={() => setConfirm(null)}
        onConfirm={() => { confirm.onConfirm?.(); setConfirm(null); }}
      />
      <LogsModal view={logView} onClose={() => setLogView(null)} />
      <KPIDetail kpi={kpiDetail} onClose={() => setKpiDetail(null)} />
      {toasts.ui}
    </div>
  );
}

export default function App() {
  return (
    <AuthGate>
      {({ onLogout }) => <DashboardApp onLogout={onLogout} />}
    </AuthGate>
  );
}
