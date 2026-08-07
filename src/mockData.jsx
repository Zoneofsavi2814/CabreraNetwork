/* Mock data — 60 points (1-hour window) for time-series, plus all panels' content. */
/* Demo fixture only — not an authoritative health or monitor contract. */

// Deterministic-ish RNG so visuals stay stable across reloads
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function series(seed, n, base, variance, drift = 0) {
  const r = rng(seed);
  const out = [];
  let v = base;
  for (let i = 0; i < n; i++) {
    v += (r() - 0.5) * variance + drift;
    // soft-clamp so values feel realistic but don't get pinned
    v = Math.max(base - variance * 4, Math.min(base + variance * 4, v));
    out.push(v);
  }
  return out;
}

function seriesBound(seed, n, min, max, smoothness = 0.65) {
  const r = rng(seed);
  let v = min + (max - min) * r();
  const out = [];
  for (let i = 0; i < n; i++) {
    const target = min + (max - min) * r();
    v = v * smoothness + target * (1 - smoothness);
    out.push(v);
  }
  return out;
}

const HISTORY = {
  cpu:        seriesBound(101, 60, 12, 38),
  ram:        seriesBound(202, 60, 38, 46, 0.78),
  temp:       seriesBound(303, 60, 48, 56, 0.80),
  ssdPct:     seriesBound(404, 60, 21.0, 21.2, 0.95),
  dnsPerMin:  seriesBound(505, 60, 220, 380),
  loadAvg:    seriesBound(606, 60, 0.35, 0.85),
  netIn:      seriesBound(707, 60, 80, 420),
  netOut:     seriesBound(808, 60, 40, 220),
  dnsTotal:   seriesBound(909, 60, 240, 410),
  dnsBlocked: seriesBound(910, 60, 50, 110),
  diskRead:   seriesBound(1010, 60, 0.2, 8),
  diskWrite:  seriesBound(1011, 60, 0.1, 4.5),
};

const KPIS = [
  { id: "cpu",   label: "CPU",         glyph: "cpu",            value: 23,    suffix: "%",  status: "ok",   delta: "+3%",   deltaTone: "neutral", history: HISTORY.cpu },
  { id: "ram",   label: "Memory",      glyph: "ram",            value: 41,    suffix: "%",  status: "ok",   delta: "-1%",   deltaTone: "down",    history: HISTORY.ram, sub: "1680 / 4096 MB" },
  { id: "temp",  label: "Temperature", glyph: "thermo",         value: 52.4,  suffix: "°C", status: "ok",   delta: "+0.8°", deltaTone: "neutral", history: HISTORY.temp },
  { id: "ssd",   label: "Data HDD Used", glyph: "disk",          value: 21.1,  suffix: "%",  status: "ok",   delta: "+0.1%", deltaTone: "neutral", history: HISTORY.ssdPct, sub: "380 / 1800 GB" },
  { id: "dns",   label: "DNS / min",   glyph: "dns",            value: 307,   suffix: "",   status: "ok",   delta: "+12%",  deltaTone: "up",      history: HISTORY.dnsPerMin },
];

const SERVICES = [
  { id: "adguard", label: "AdGuard Home",  unit: "AdGuardHome.service",         port: "53 · 8080", status: "ok", glyph: "brandShield",     ui: "http://192.168.0.101:8080" },
  { id: "k3s",     label: "k3s",           unit: "k3s.service",                 port: "6443",      status: "ok", glyph: "brandCubes" },
  { id: "portfolio", label: "CabreraPortfolio", unit: "cabrera-portfolio.service", port: "8099", status: "ok", glyph: "activity", ui: "https://ann-and-chris.tail83be27.ts.net:9443/" },
  { id: "programs", label: "CabreraPrograms", unit: "cabrera-programs.service", port: "8096", status: "ok", glyph: "brandTerminal", ui: "http://192.168.0.101:8096" },
  { id: "grid",    label: "GRID",          kind: "k3s", namespace: "homelab", workloadKind: "deployment", workload: "grid",        unit: "k3s · homelab/grid",        port: "8090 · 7777", status: "ok", glyph: "globe",          ui: "http://192.168.0.101:8090" },
  { id: "smbd",    label: "Samba (smbd)",  unit: "smbd.service",                port: "445",       status: "ok", glyph: "brandFolderNet" },
  { id: "nmbd",    label: "Samba (nmbd)",  unit: "nmbd.service",                port: "139",       status: "ok", glyph: "brandFolderNet" },
  { id: "ssh",     label: "SSH",           unit: "ssh.service",                 port: "22",        status: "ok", glyph: "brandTerminal" },
];

const WEB_APPS = [
  { id: "cabrera-network", label: "Cabrera Network", url: "https://dashboard.example.tailnet/", port: "443", glyph: "activity", kind: "dashboard", status: "ok", statusLabel: "online" },
  { id: "adguard", label: "AdGuard Home", url: "http://192.168.0.101:8080/", port: "8080", glyph: "brandShield", kind: "admin", status: "ok", statusLabel: "online" },
  { id: "grid-wiki", label: "GRID Wiki", url: "http://192.168.0.101:8090/", port: "8090", glyph: "globe", kind: "knowledge", status: "ok", statusLabel: "online" },
  { id: "grid-api", label: "GRID protected listener/API", url: "http://192.168.0.101:7777/", port: "7777", glyph: "brandSocket", kind: "api", status: "ok", statusLabel: "online" },
];

const ADGUARD = {
  queries: 18420,
  blocked: 4310,
  blockRatio: 23.4,
  upstream: "cloudflare-dns.com",
  topDomains: [
    { name: "doubleclick.net",         count: 412 },
    { name: "googlesyndication.com",   count: 380 },
    { name: "adservice.google.com",    count: 274 },
    { name: "scorecardresearch.com",   count: 198 },
    { name: "analytics.tiktok.com",    count: 156 },
  ],
  topClients: [
    { name: "iphone-cab",  ip: "192.168.0.42",  count: 5210 },
    { name: "macbook",     ip: "192.168.0.55",  count: 4112 },
    { name: "pi4",         ip: "192.168.0.101", count: 1890 },
    { name: "appletv",     ip: "192.168.0.61",  count: 1304 },
    { name: "kindle",      ip: "192.168.0.78",  count:  642 },
  ],
};

const K3S = {
  version: "v1.30.4+k3s1",
  nodes: [
    { name: "pi4", role: "control-plane,master", version: "v1.30.4+k3s1", ready: true, age: "16d" },
  ],
  podsByNs: [
    { ns: "kube-system", running: 6, pending: 0, failed: 0 },
    { ns: "default",     running: 2, pending: 0, failed: 0 },
    { ns: "monitoring",  running: 3, pending: 0, failed: 0 },
    { ns: "ingress-nginx", running: 1, pending: 0, failed: 0 },
  ],
  events: [
    { t: "12:04:11", kind: "Normal", reason: "Pulled",   obj: "pod/web-deployment-7c4d",  msg: "Successfully pulled image \"echo:1.2\"" },
    { t: "12:04:09", kind: "Normal", reason: "Created",  obj: "pod/web-deployment-7c4d",  msg: "Created container web" },
    { t: "11:58:02", kind: "Normal", reason: "Started",  obj: "pod/web-deployment-7c4d",  msg: "Started container web" },
    { t: "11:42:18", kind: "Normal", reason: "Scheduled",obj: "pod/grafana-agent-x9m2",   msg: "Successfully assigned monitoring/grafana-agent to pi4" },
    { t: "10:11:30", kind: "Normal", reason: "Pulled",   obj: "pod/coredns-58b8b8c879",   msg: "Container image already present on machine" },
  ],
};

const STORAGE = {
  root:   { used: 12,  total: 32,   fs: "ext4", mount: "/" },
  ssd:    { label: "Data HDD", media: "HDD", model: "WDC WD20SDRW-11VUUS1", device: "/dev/sda1", rotational: true, transport: "USB", used: 380, total: 1800, fs: "ext4", mount: "/mnt/ssd", segments: [
    { label: "nas",    value: 312, tone: "ok" },
    { label: "other",  value: 68,  tone: "muted" },
  ]},
};

const LOGS = [
  { t: "12:04:11", src: "k3s",     level: "info", msg: "Started container web-deployment-7c4d" },
  { t: "12:01:53", src: "AdGuard", level: "info", msg: "Blocked doubleclick.net for 192.168.0.42" },
  { t: "12:00:14", src: "AdGuard", level: "info", msg: "Blocked googlesyndication.com for 192.168.0.55" },
  { t: "11:58:02", src: "k3s",     level: "info", msg: "Created container web in pod web-deployment-7c4d" },
  { t: "11:42:18", src: "kubelet", level: "info", msg: "Successfully assigned monitoring/grafana-agent to pi4" },
  { t: "11:39:42", src: "smbd",    level: "info", msg: "192.168.0.55 connected to share nas (user nasuser)" },
];

const HOST = {
  name: "pi4",
  ip: "192.168.0.101",
  os: "Raspberry Pi OS Bookworm",
  arch: "aarch64",
  kernel: "6.12.47+rpt-rpi-v8",
  uptime: "16d 14h",
  loadAvg: [0.45, 0.62, 0.71],
  cpuPct: 23,
  ramPct: 41,
  ramUsed: 1680,
  ramTotal: 4096,
  swap: 3,
  tempC: 52.4,
};

const TOPOLOGY = {
  router: { id: "main", name: "TP-Link Archer BE400", ip: "192.168.0.1", model: "Archer BE400" },
  host: { id: "pi4", name: "pi4", ip: "192.168.0.101", linkType: "wired", interface: "Wired", glyph: "cpu" },
  source: "mock topology",
  aggregateKbps: 1240,
  counts: { total: 4, known: 6, online: 4, aps: 2, apsOnline: 2, lan: 0, wired: 1, wifi: 3, unknown: 0, mesh: 0, dnsKnown: 3 },
  updatedAt: new Date().toISOString(),
  aps: [
    { key: "mac:98:03:8e:65:a4:ec", id: "mac:98:03:8e:65:a4:ec", apId: "basement", displayName: "ArcherAX3000Pro_Basement", sourceName: "ArcherAX3000Pro_Basement", ip: "192.168.0.118", mac: "98:03:8e:65:a4:ec", linkType: "wired", interface: "Wired", online: true, isAp: true, isMesh: true, glyph: "mesh", tone: "ok", rxKbps: 44, txKbps: 12, confidence: "high", sourceText: "router", sources: ["router"], location: "Basement" },
    { key: "mac:98:03:8e:44:f7:e4", id: "mac:98:03:8e:44:f7:e4", apId: "loft", displayName: "ArcherAX3000Pro_Loft", sourceName: "ArcherAX3000Pro_Loft", ip: "192.168.0.216", mac: "98:03:8e:44:f7:e4", linkType: "wired", interface: "Wired", online: true, isAp: true, isMesh: true, glyph: "mesh", tone: "ok", rxKbps: 38, txKbps: 10, confidence: "high", sourceText: "router", sources: ["router"], location: "Loft" },
  ],
  groups: [
    { id: "wired", label: "Wired LAN", count: 1, online: 1, rxKbps: 11.1, txKbps: 7 },
    { id: "basement", label: "Basement AP", count: 1, online: 1, rxKbps: 1.2, txKbps: 0.4 },
    { id: "loft", label: "Loft AP", count: 1, online: 1, rxKbps: 320, txKbps: 84 },
    { id: "wifi-unknown", label: "Wi-Fi AP unknown", count: 1, online: 1, rxKbps: 0.8, txKbps: 0.2 },
    { id: "unknown", label: "Link unknown", count: 0, online: 0, rxKbps: 0, txKbps: 0 },
  ],
  clients: [
    { key: "mac:0e:5c:e5:70:c3:5a", id: "mac:0e:5c:e5:70:c3:5a", displayName: "Anns-MacBook", sourceName: "Anns-MacBook", name: "Anns-MacBook", ip: "192.168.0.42", mac: "0e:5c:e5:70:c3:5a", linkType: "5g", interface: "5G", apId: "loft", groupId: "loft", online: true, glyph: "laptop", tone: "ok", rxKbps: 320, txKbps: 84, queryCount: 5210, confidence: "high", sourceText: "adguard + router", sources: ["adguard", "router"] },
    { key: "mac:8c:2a:85:71:ae:16", id: "mac:8c:2a:85:71:ae:16", displayName: "Android", sourceName: "Android", name: "Android", ip: "192.168.0.146", mac: "8c:2a:85:71:ae:16", linkType: "wired", interface: "Wired", groupId: "wired", online: true, glyph: "phone", tone: "ok", rxKbps: 11.1, txKbps: 7, queryCount: 84, confidence: "high", sourceText: "router", sources: ["router"] },
    { key: "mac:a0:d2:b1:14:c4:f6", id: "mac:a0:d2:b1:14:c4:f6", displayName: "AmazonPlug164L", sourceName: "AmazonPlug164L", name: "AmazonPlug164L", ip: "192.168.0.225", mac: "a0:d2:b1:14:c4:f6", linkType: "2.4g", interface: "2.4G", apId: "basement", groupId: "basement", online: true, glyph: "iot", tone: "ok", rxKbps: 1.2, txKbps: 0.4, queryCount: 124, confidence: "high", sourceText: "adguard + router", sources: ["adguard", "router"] },
    { key: "mac:fc:9c:98:91:3a:a2", id: "mac:fc:9c:98:91:3a:a2", displayName: "AVD4001-03824", sourceName: "AVD4001-03824", name: "AVD4001-03824", ip: "192.168.0.251", mac: "fc:9c:98:91:3a:a2", linkType: "2.4g", interface: "2.4G", groupId: "wifi-unknown", online: true, glyph: "device", tone: "ok", rxKbps: 0.8, txKbps: 0.2, queryCount: 33, confidence: "high", sourceText: "router", sources: ["router"] },
  ],
};

const OPS_CENTER = {
  schemaVersion: 1,
  updatedAt: new Date().toISOString(),
  summary: { status: "warn", ok: 25, warn: 1, fail: 0, total: 26, message: "0 failed · 1 warning", nextRunAt: new Date(Date.now() + 180000).toISOString() },
  cadences: [
    {
      id: "five-minute",
      label: "Every 5 minutes",
      intervalSeconds: 300,
      glyph: "activity",
      status: "warn",
      lastRunAt: new Date().toISOString(),
      nextRunAt: new Date(Date.now() + 300000).toISOString(),
      durationMs: 214,
      checks: [
        { id: "grid-web", label: "GRID web/API", host: "Pi4 k3s", kind: "http", status: "ok", message: "ok=true", latencyMs: 24, href: "http://192.168.0.101:8090/healthz" },
        { id: "coinbot", label: "Coinbot API", host: "Pi5 k3s", kind: "http", status: "warn", message: "degraded=true", latencyMs: 42, href: "http://192.168.0.94:8787/health" },
        { id: "wan-http", label: "WAN HTTPS reachability", host: "Internet", kind: "http", status: "ok", message: "HTTP 200", latencyMs: 58, href: "https://one.one.one.one/cdn-cgi/trace" },
        { id: "wan-latency", label: "WAN endpoint latency", host: "Internet", kind: "multi-http", status: "ok", message: "3/3 endpoints, avg 213ms", latencyMs: 641 },
        { id: "dns-latency", label: "DNS latency", host: "Pi4", kind: "multi-dns", status: "ok", message: "3/3 names, avg 34ms", latencyMs: 102 },
        { id: "portfolio-api", label: "Portfolio API", host: "Pi5", kind: "http", status: "ok", message: "ok=true", latencyMs: 35, href: "https://ann-and-chris.tail83be27.ts.net:9443/api/health" },
      ],
    },
    {
      id: "hourly",
      label: "Every hour",
      intervalSeconds: 3600,
      glyph: "clock",
      status: "ok",
      lastRunAt: new Date().toISOString(),
      nextRunAt: new Date(Date.now() + 3600000).toISOString(),
      durationMs: 180,
      checks: [
        { id: "wan-speed", label: "WAN speed sample", host: "Internet", kind: "speed-lite", status: "ok", message: "211.4 Mbps sample", latencyMs: 92 },
        { id: "k3s-release", label: "k3s latest release", host: "GitHub", kind: "github-release", status: "ok", message: "latest v1.35.5+k3s1", latencyMs: 118, href: "https://github.com/k3s-io/k3s/releases/latest" },
        { id: "hourly-backups", label: "Backup verification", host: "Pi4", kind: "backup-recent", status: "ok", message: "newest 2.1h ago, 8 files, 128 KB", latencyMs: 4 },
        { id: "backup-artifacts", label: "Backup artifact integrity", host: "Pi4", kind: "backup-artifacts", status: "ok", message: "4 archives readable, 1 checksums verified, 2 external skipped", latencyMs: 26 },
        { id: "pi4-k3s-apps", label: "Pi4 k3s apps", host: "Pi4 k3s", kind: "k3s-local", status: "ok", message: "1/1 workloads ready", latencyMs: 3 },
        { id: "pi5-k3s-node", label: "Pi5 k3s node", host: "Pi5 k3s", kind: "ssh-k3s", status: "ok", message: "1/1 nodes ready", latencyMs: 164 },
        { id: "pi5-k3s-apps", label: "Pi5 k3s apps", host: "Pi5 k3s", kind: "ssh-k3s", status: "ok", message: "3/3 workloads ready", latencyMs: 184 },
      ],
    },
    {
      id: "morning",
      label: "Every morning",
      intervalSeconds: 86400,
      scheduleTime: "07:00",
      glyph: "bell",
      status: "ok",
      lastRunAt: new Date().toISOString(),
      nextRunAt: new Date(Date.now() + 86400000).toISOString(),
      durationMs: 4,
      checks: [
        { id: "brief", label: "Morning service brief", host: "Ops Center", kind: "operations-brief", status: "ok", message: "brief generated: 1 warning in current checks", latencyMs: 1 },
        { id: "overnight-storage-events", label: "Overnight storage events", host: "Pi4", kind: "journal-pattern", status: "ok", message: "no matching events since 12 hours ago", latencyMs: 12 },
      ],
    },
    {
      id: "nightly",
      label: "Every night",
      intervalSeconds: 86400,
      scheduleTime: "23:55",
      glyph: "moon",
      status: "ok",
      lastRunAt: new Date().toISOString(),
      nextRunAt: new Date(Date.now() + 86400000).toISOString(),
      durationMs: 93,
      checks: [
        { id: "logrotate-timer", label: "Log rotation timer", host: "Pi4", kind: "systemd-timer", status: "ok", message: "active, last 2.0h ago", latencyMs: 4 },
        { id: "log2ram-flush", label: "log2ram daily flush", host: "Pi4", kind: "systemd-timer", status: "ok", message: "active, last 2.1h ago", latencyMs: 5 },
        { id: "tmpfiles-clean", label: "Temp/log cleanup timer", host: "Pi4", kind: "systemd-timer", status: "ok", message: "active, last 6.8h ago", latencyMs: 3 },
        { id: "kernel-io-health", label: "Kernel storage errors", host: "Pi4", kind: "journal-pattern", status: "ok", message: "no matching events since 24 hours ago", latencyMs: 16 },
        { id: "root-disk", label: "Root disk headroom", host: "Pi4", kind: "disk", status: "ok", message: "8.3% used, 4.0% inodes, rw", latencyMs: 1 },
        { id: "data-hdd-disk", label: "Data HDD headroom", host: "Pi4", kind: "disk", status: "ok", message: "0.3% used, 1.0% inodes, rw", latencyMs: 1 },
        { id: "brain-freshness", label: "GRID brain freshness", host: "Pi4", kind: "path-freshness", status: "ok", message: "newest 0.2h ago", latencyMs: 8 },
        { id: "brain-vault-parity", label: "GRID vault path parity", host: "Pi4", kind: "path-parity", status: "ok", message: "42 source, 42 target", latencyMs: 19 },
        { id: "grid-vault-sync", label: "GRID vault/index sync", host: "Pi4 k3s", kind: "grid-sync", status: "ok", message: "42 notes, scan 0.1m ago", latencyMs: 32 },
        { id: "backup-retention", label: "Backup retention pressure", host: "Pi4", kind: "directory-retention", status: "ok", message: "14 entries, oldest 58d", latencyMs: 2 },
      ],
    },
  ],
  events: [
    { t: "06:12:09", tone: "warn", source: "Pi5 k3s", msg: "Coinbot API: degraded=true" },
  ],
};

export const DEMO_DASHBOARD = { HISTORY, KPIS, SERVICES, WEB_APPS, ADGUARD, K3S, STORAGE, LOGS, HOST, TOPOLOGY, OPS_CENTER };
