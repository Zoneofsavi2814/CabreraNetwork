/* Mock data — 60 points (1-hour window) for time-series, plus all panels' content. */

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
  containers: Array(60).fill(8).map((v, i) => i > 45 ? 9 : v),
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
  { id: "ssd",   label: "SSD Used",    glyph: "disk",           value: 21.1,  suffix: "%",  status: "ok",   delta: "+0.1%", deltaTone: "neutral", history: HISTORY.ssdPct, sub: "380 / 1800 GB" },
  { id: "dns",   label: "DNS / min",   glyph: "dns",            value: 307,   suffix: "",   status: "ok",   delta: "+12%",  deltaTone: "up",      history: HISTORY.dnsPerMin },
  { id: "ctn",   label: "Containers",  glyph: "containerStack", value: "8/8", suffix: "",   status: "ok",   delta: "+1",    deltaTone: "up",      history: HISTORY.containers },
];

const SERVICES = [
  { id: "adguard", label: "AdGuard Home",  unit: "AdGuardHome.service",         port: "53 · 8080", status: "ok", glyph: "brandShield",     ui: "http://192.168.0.101:8080" },
  { id: "k3s",     label: "k3s",           unit: "k3s.service",                 port: "6443",      status: "ok", glyph: "brandCubes" },
  { id: "kuma",    label: "Uptime Kuma",   unit: "container-uptime-kuma.service",port: "3001",     status: "ok", glyph: "brandHeartbeat",  ui: "http://192.168.0.101:3001" },
  { id: "esty",    label: "Esty",          unit: "container-esty.service",      port: "8095",      status: "ok", glyph: "brandContainer",  ui: "http://192.168.0.101:8095" },
  { id: "smbd",    label: "Samba (smbd)",  unit: "smbd.service",                port: "445",       status: "ok", glyph: "brandFolderNet" },
  { id: "nmbd",    label: "Samba (nmbd)",  unit: "nmbd.service",                port: "139",       status: "ok", glyph: "brandFolderNet" },
  { id: "ssh",     label: "SSH",           unit: "ssh.service",                 port: "22",        status: "ok", glyph: "brandTerminal" },
  { id: "podman",  label: "Podman socket", unit: "podman.socket",               port: "—",         status: "ok", glyph: "brandSocket" },
];

const CONTAINERS = [
  { id: "uptime-kuma",   image: "docker.io/louislam/uptime-kuma:1", status: "running", uptime: "168 h", restarts: 0, cpu: 0.4, mem: 92,  status_tone: "ok" },
  { id: "coinbot",       image: "localhost/cabrera-mission-control:latest", status: "running", uptime: "5 h", restarts: 0, cpu: 0.3, mem: 88, status_tone: "ok" },
  { id: "Coinbot-Mission-Control", label: "Coinbot-Mission-Control", image: "localhost/cabrera-mission-control:latest", status: "running", uptime: "5 h", restarts: 0, cpu: 0.5, mem: 132, status_tone: "ok" },
  { id: "grid",          image: "localhost/grid:v1",                 status: "running", uptime: "19 h", restarts: 0, cpu: 0.2, mem: 41,  status_tone: "ok" },
  { id: "esty", label: "Esty", image: "localhost/esty:latest", status: "running", uptime: "2 h", restarts: 0, cpu: 0.2, mem: 74, status_tone: "ok" },
];

const WEB_APPS = [
  { id: "cabrera-network", label: "Cabrera Network", url: "http://192.168.0.101/", port: "80", glyph: "activity", kind: "dashboard", status: "ok", statusLabel: "listening" },
  { id: "adguard", label: "AdGuard Home", url: "http://192.168.0.101:8080/", port: "8080", glyph: "brandShield", kind: "admin", status: "ok", statusLabel: "listening" },
  { id: "uptime-kuma", label: "Uptime Kuma", url: "http://192.168.0.101:3001/", port: "3001", glyph: "brandHeartbeat", kind: "monitoring", status: "ok", statusLabel: "listening" },
  { id: "coinbot-mission-control", label: "Coinbot-Mission-Control", url: "http://192.168.0.101:8088/", port: "8088", glyph: "brandContainer", kind: "control", status: "ok", statusLabel: "listening" },
  { id: "grid-wiki", label: "GRID Wiki", url: "http://192.168.0.101:8090/", port: "8090", glyph: "globe", kind: "knowledge", status: "ok", statusLabel: "listening" },
  { id: "grid-api", label: "GRID protected listener/API", url: "http://192.168.0.101:7777/", port: "7777", glyph: "brandSocket", kind: "api", status: "ok", statusLabel: "listening" },
  { id: "esty", label: "Esty", url: "http://192.168.0.101:8095/", port: "8095", glyph: "brandContainer", kind: "app", status: "ok", statusLabel: "listening" },
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
  ssd:    { used: 380, total: 1800, fs: "ext4", mount: "/mnt/ssd", segments: [
    { label: "podman", value: 24,  tone: "cyan" },
    { label: "nas",    value: 312, tone: "ok" },
    { label: "other",  value: 44,  tone: "muted" },
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

const PI5_CODEX = {
  label: "Raspberry Pi 5 Codex",
  host: "Raspberry Pi 5",
  ip: "192.168.0.94",
  user: "pi5",
  sshTarget: "pi5@192.168.0.94",
  binary: "/home/pi5/.npm-global/bin/codex",
  workingDir: "/home/pi5",
  status: "ready",
};

const TOPOLOGY = {
  router: { id: "main", name: "TP-Link Archer BE400", ip: "192.168.0.1", model: "Archer BE400" },
  host: { id: "pi4", name: "pi4", ip: "192.168.0.101", linkType: "wired", interface: "Wired", glyph: "cpu" },
  source: "mock topology",
  aggregateKbps: 1240,
  counts: { total: 4, known: 6, online: 4, aps: 2, apsOnline: 2, lan: 0, wired: 1, wifi: 3, unknown: 0, mesh: 0, dnsKnown: 3 },
  updatedAt: new Date().toISOString(),
  aps: [
    { key: "mac:98:03:8e:65:a4:ec", id: "mac:98:03:8e:65:a4:ec", apId: "basement", displayName: "ArcherAX3000Pro_Basement", sourceName: "ArcherAX3000Pro_Basement", ip: "192.168.0.117", mac: "98:03:8e:65:a4:ec", linkType: "wired", interface: "Wired", online: true, isAp: true, isMesh: true, glyph: "mesh", tone: "ok", rxKbps: 44, txKbps: 12, confidence: "high", sourceText: "router", sources: ["router"], location: "Basement" },
    { key: "mac:98:03:8e:44:f7:e4", id: "mac:98:03:8e:44:f7:e4", apId: "loft", displayName: "ArcherAX3000Pro_Loft", sourceName: "ArcherAX3000Pro_Loft", ip: "192.168.0.176", mac: "98:03:8e:44:f7:e4", linkType: "wired", interface: "Wired", online: true, isAp: true, isMesh: true, glyph: "mesh", tone: "ok", rxKbps: 38, txKbps: 10, confidence: "high", sourceText: "router", sources: ["router"], location: "Loft" },
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

export const DEFAULT_DASHBOARD = { HISTORY, KPIS, SERVICES, CONTAINERS, WEB_APPS, ADGUARD, K3S, STORAGE, LOGS, HOST, PI5_CODEX, TOPOLOGY };
