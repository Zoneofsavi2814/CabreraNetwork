import React from "react";

/* Chart primitives — sparkline, multi-series line, line+bars, donut, hbar.
   All hand-rolled SVG, deterministic, no NaN states. Subtle gradient fills allowed. */

function pathFromSeries(values, w, h, padX = 0, padY = 2) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = (max - min) || 1;
  const innerW = w - padX * 2;
  const innerH = h - padY * 2;
  const stepX = innerW / (values.length - 1);
  return values.map((v, i) => {
    const x = padX + i * stepX;
    const y = padY + innerH - ((v - min) / range) * innerH;
    return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

function areaPathFromSeries(values, w, h, padX = 0, padY = 2) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = (max - min) || 1;
  const innerW = w - padX * 2;
  const innerH = h - padY * 2;
  const stepX = innerW / (values.length - 1);
  let d = "";
  values.forEach((v, i) => {
    const x = padX + i * stepX;
    const y = padY + innerH - ((v - min) / range) * innerH;
    d += (i === 0 ? "M" : "L") + ` ${x.toFixed(2)} ${y.toFixed(2)} `;
  });
  d += `L ${padX + innerW} ${h - padY} L ${padX} ${h - padY} Z`;
  return d;
}

export const Sparkline = ({ values, color = "var(--cyan)", height = 28, width = 220, fill = true }) => {
  const id = React.useId().replace(/:/g, "");
  const p = pathFromSeries(values, width, height);
  const a = areaPathFromSeries(values, width, height);
  return (
    <svg className="spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ height }}>
      {fill && (
        <>
          <defs>
            <linearGradient id={`g-${id}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.36" />
              <stop offset="55%" stopColor={color} stopOpacity="0.1" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={a} fill={`url(#g-${id})`} />
        </>
      )}
      <path
        d={p} fill="none" stroke={color} strokeWidth="1.6"
        strokeLinejoin="round" strokeLinecap="round"
        style={{ filter: `drop-shadow(0 0 3px ${color})` }}
      />
    </svg>
  );
};

/* Time-series chart with axis. seriesList = [{values, color, label, area?}] */
export const TimeSeries = ({ seriesList, height = 120, yLabel, bars }) => {
  const W = 700, H = height;
  const padL = 36, padR = 8, padT = 12, padB = 18;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const all = seriesList.flatMap(s => s.values).concat(bars ? bars.values : []);
  const min = 0;
  const max = Math.max(...all) * 1.1 || 1;
  const N = seriesList[0].values.length;
  const stepX = innerW / (N - 1);
  const ticks = 4;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
      {/* grid */}
      <g className="chart-grid">
        {Array.from({ length: ticks + 1 }).map((_, i) => {
          const y = padT + (innerH / ticks) * i;
          const val = max - ((max - min) / ticks) * i;
          return (
            <g key={i}>
              <line x1={padL} x2={W - padR} y1={y} y2={y} />
              <text x={padL - 8} y={y + 3} textAnchor="end" className="chart-axis-label">
                {val >= 100 ? Math.round(val) : val.toFixed(1)}
              </text>
            </g>
          );
        })}
      </g>
      {/* bars (e.g. blocked DNS) — drawn before lines so lines sit on top */}
      {bars && (() => {
        const bw = Math.max(2, stepX * 0.55);
        return bars.values.map((v, i) => {
          const x = padL + i * stepX - bw / 2;
          const h = (v / max) * innerH;
          const y = padT + innerH - h;
          return <rect key={i} x={x} y={y} width={bw} height={h} fill={bars.color} opacity="0.7" rx="1.5" />;
        });
      })()}
      {/* areas + lines */}
      {seriesList.map((s, idx) => {
        const id = `area-${idx}-${React.useId().replace(/:/g, "")}`;
        const path = s.values.map((v, i) => {
          const x = padL + i * stepX;
          const y = padT + innerH - (v / max) * innerH;
          return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
        }).join(" ");
        const area = path + ` L ${padL + innerW} ${padT + innerH} L ${padL} ${padT + innerH} Z`;
        return (
          <g key={idx}>
            {s.area !== false && (
              <>
                <defs>
                  <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stopColor={s.color} stopOpacity="0.32" />
                    <stop offset="60%" stopColor={s.color} stopOpacity="0.08" />
                    <stop offset="100%" stopColor={s.color} stopOpacity="0" />
                  </linearGradient>
                </defs>
                <path d={area} fill={`url(#${id})`} />
              </>
            )}
            <path
              d={path} fill="none" stroke={s.color} strokeWidth="1.8" strokeLinejoin="round"
              style={{ filter: `drop-shadow(0 0 4px ${s.color})` }}
            />
          </g>
        );
      })}
      {/* x-axis tick labels (now / -30m / -60m) */}
      <text x={padL} y={H - 4} className="chart-axis-label" textAnchor="start">-60m</text>
      <text x={padL + innerW / 2} y={H - 4} className="chart-axis-label" textAnchor="middle">-30m</text>
      <text x={W - padR} y={H - 4} className="chart-axis-label" textAnchor="end">now</text>
    </svg>
  );
};

/* Donut for block ratio */
export const Donut = ({ value, total, color = "var(--emerald)", size = 140, label, sublabel }) => {
  const stroke = 14;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = total > 0 ? value / total : 0;
  return (
    <div style={{ position: "relative", width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="rgba(120,180,220,0.12)" strokeWidth={stroke} />
        <circle
          cx={size/2} cy={size/2} r={r}
          fill="none" stroke={color} strokeWidth={stroke}
          strokeDasharray={`${c * pct} ${c}`}
          strokeDashoffset={c / 4}
          strokeLinecap="round"
          transform={`rotate(-90 ${size/2} ${size/2})`}
          style={{ filter: `drop-shadow(0 0 6px ${color})`, transition: "stroke-dasharray 600ms cubic-bezier(.16,1,.3,1)" }}
        />
      </svg>
      <div style={{
        position: "absolute", inset: 0, display: "flex",
        flexDirection: "column", alignItems: "center", justifyContent: "center",
        gap: 2,
      }}>
        <div className="mono" style={{ fontSize: 28, fontWeight: 500, letterSpacing: -0.5 }}>{label}</div>
        <div style={{ fontSize: 10, color: "var(--slate)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{sublabel}</div>
      </div>
    </div>
  );
};

/* Horizontal bar list */
export const HBar = ({ items, max, color = "var(--cyan)", valueFmt = (v) => v.toLocaleString() }) => {
  const M = max ?? Math.max(...items.map(i => i.count));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {items.map((it, i) => {
        const pct = (it.count / M) * 100;
        return (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 10, alignItems: "center" }}>
            <div style={{ minWidth: 0 }}>
              <div style={{
                display: "flex", alignItems: "baseline",
                fontSize: 11, marginBottom: 4, gap: 8,
              }}>
                <span style={{
                  flex: 1, minWidth: 0,
                  whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                  color: "var(--fg-dim)",
                }}>
                  {it.name}
                </span>
                {it.ip && <span className="mono" style={{ color: "var(--slate-2)", fontSize: 10, flexShrink: 0, whiteSpace: "nowrap" }}>{it.ip}</span>}
              </div>
              <div style={{
                height: 5, background: "rgba(120,180,220,0.08)",
                borderRadius: 3, overflow: "hidden",
              }}>
                <div style={{
                  height: "100%", width: `${pct}%`,
                  background: `linear-gradient(90deg, rgba(255,255,255,0) 30%, rgba(255,255,255,0.28)), ${color}`,
                  borderRadius: 3,
                  boxShadow: `0 0 8px -1px ${color}`,
                  transition: "width 480ms cubic-bezier(.16,1,.3,1)",
                }} />
              </div>
            </div>
            <div className="mono" style={{ fontSize: 12, color: "var(--fg)", minWidth: 48, textAlign: "right" }}>
              {valueFmt(it.count)}
            </div>
          </div>
        );
      })}
    </div>
  );
};

/* Stacked horizontal bar for storage */
export const StackedBar = ({ segments, total, height = 12 }) => {
  // segments: [{label, value, tone}]
  const tones = {
    ok: "var(--emerald)",
    warn: "var(--amber)",
    fail: "var(--rose)",
    cyan: "var(--cyan)",
    muted: "var(--slate-3)",
  };
  return (
    <div style={{
      display: "flex", height, borderRadius: 6, overflow: "hidden",
      border: "1px solid var(--hairline)", background: "rgba(148,163,184,0.04)",
    }}>
      {segments.map((s, i) => {
        const pct = (s.value / total) * 100;
        return (
          <div key={i} title={`${s.label}: ${s.value} GB`} style={{
            width: `${pct}%`,
            background: `linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0)), ${tones[s.tone] || tones.cyan}`,
            opacity: 0.95,
            borderRight: i < segments.length - 1 ? "1px solid rgba(0,0,0,0.35)" : "none",
          }} />
        );
      })}
    </div>
  );
};

/* Stacked bar (vertical) for pods-by-namespace */
export const PodsBar = ({ items }) => {
  const max = Math.max(...items.map(i => i.running + i.pending + i.failed)) * 1.2 || 1;
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 18, height: 140, padding: "8px 0" }}>
      {items.map((it, i) => {
        const total = it.running + it.pending + it.failed;
        const hPx = (total / max) * 110;
        const rPx = total > 0 ? (it.running / total) * hPx : 0;
        const pPx = total > 0 ? (it.pending / total) * hPx : 0;
        const fPx = total > 0 ? (it.failed  / total) * hPx : 0;
        return (
          <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6, flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 11, color: "var(--fg)" }}>{total}</div>
            <div style={{
              width: 28, height: hPx, display: "flex", flexDirection: "column-reverse",
              borderRadius: 4, overflow: "hidden", border: "1px solid var(--hairline)",
            }}>
              <div style={{ height: rPx, background: "var(--emerald)", opacity: 0.92, boxShadow: "inset 0 0 8px -2px var(--emerald)" }} />
              <div style={{ height: pPx, background: "var(--amber)",   opacity: 0.92 }} />
              <div style={{ height: fPx, background: "var(--rose)",    opacity: 0.92 }} />
            </div>
            <div style={{
              fontSize: 10, color: "var(--slate)",
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              maxWidth: 80, textAlign: "center",
            }}>{it.ns}</div>
          </div>
        );
      })}
    </div>
  );
};
