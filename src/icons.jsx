/* Icons — single 16px stroke family. Brand glyphs are original, simplified
   stylizations (shield / stacked cubes / cargo box / folder-with-network-line /
   terminal prompt) — not the real product logos. */

import React from "react";

export const Icon = ({ name, size = 16, strokeWidth = 1.6, ...rest }) => {
  const P = paths[name];
  if (!P) return null;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {P}
    </svg>
  );
};

const paths = {
  /* Status */
  check:   <><polyline points="20 6 9 17 4 12"/></>,
  alert:   <><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></>,
  x:       <><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></>,
  dashed:  <><circle cx="12" cy="12" r="9" strokeDasharray="3 3"/></>,
  info:    <><circle cx="12" cy="12" r="9"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></>,

  /* Actions */
  play:    <><polygon points="6 4 20 12 6 20 6 4" fill="currentColor" stroke="none"/></>,
  stop:    <><rect x="6" y="6" width="12" height="12" rx="1" fill="currentColor" stroke="none"/></>,
  restart: <><path d="M3 12a9 9 0 1 0 3-6.7"/><polyline points="3 4 3 9 8 9"/></>,
  logs:    <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="14" y2="17"/></>,
  external:<><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></>,
  refresh: <><path d="M21 12a9 9 0 1 1-3-6.7"/><polyline points="21 3 21 9 15 9"/></>,
  search:  <><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></>,
  bell:    <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></>,
  moon:    <><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></>,
  command: <><path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/></>,
  more:    <><circle cx="5"  cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="19" cy="12" r="1.4" fill="currentColor"/></>,
  chevron: <><polyline points="9 6 15 12 9 18"/></>,
  arrowUp:   <><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></>,
  arrowDown: <><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></>,
  density: <><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></>,
  sliders: <><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></>,

  /* Metric glyphs */
  cpu:     <><rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="0.5"/><line x1="9" y1="2" x2="9" y2="5"/><line x1="15" y1="2" x2="15" y2="5"/><line x1="9" y1="19" x2="9" y2="22"/><line x1="15" y1="19" x2="15" y2="22"/><line x1="2" y1="9" x2="5" y2="9"/><line x1="2" y1="15" x2="5" y2="15"/><line x1="19" y1="9" x2="22" y2="9"/><line x1="19" y1="15" x2="22" y2="15"/></>,
  ram:     <><rect x="3" y="8" width="18" height="9" rx="1"/><line x1="7" y1="8" x2="7" y2="17"/><line x1="11" y1="8" x2="11" y2="17"/><line x1="15" y1="8" x2="15" y2="17"/><line x1="19" y1="8" x2="19" y2="17"/><line x1="5" y1="6" x2="5" y2="8"/><line x1="9" y1="6" x2="9" y2="8"/><line x1="13" y1="6" x2="13" y2="8"/><line x1="17" y1="6" x2="17" y2="8"/></>,
  thermo:  <><path d="M14 14.76V4a2 2 0 1 0-4 0v10.76a4 4 0 1 0 4 0z"/></>,
  disk:    <><ellipse cx="12" cy="6" rx="9" ry="3"/><path d="M3 6v6a9 3 0 0 0 18 0V6"/><path d="M3 12v6a9 3 0 0 0 18 0v-6"/></>,
  dns:     <><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></>,
  containerStack: <><rect x="3" y="3" width="18" height="6" rx="1"/><rect x="3" y="11" width="18" height="6" rx="1"/><line x1="7" y1="6" x2="7" y2="6.01"/><line x1="7" y1="14" x2="7" y2="14.01"/></>,
  network: <><circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M12 7v4M12 11l-7 6M12 11l7 6"/></>,
  activity:<><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></>,
  clock:   <><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></>,
  zap:     <><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" fill="currentColor" stroke="none"/></>,

  /* Brand glyphs v2 — original monoline marks, 16px family.
     Inspired by the real products, drawn from scratch. */

  // AdGuard-style: rounded shield with a centered horizontal "block" bar
  brandShield: <>
    <path d="M12 3.2c-2.5 1.4-5 2-7.5 2v6.6c0 4.4 3 7.5 7.5 9 4.5-1.5 7.5-4.6 7.5-9V5.2c-2.5 0-5-.6-7.5-2z"/>
    <line x1="8.5" y1="12" x2="15.5" y2="12"/>
  </>,

  // k3s-style: a single isometric cube ("k3s" is k8s slimmed — one cube, not many)
  brandCubes: <>
    <path d="M12 3.5l7.5 4.25v8.5L12 20.5 4.5 16.25v-8.5L12 3.5z"/>
    <path d="M4.5 7.75L12 12l7.5-4.25"/>
    <line x1="12" y1="12" x2="12" y2="20.5"/>
  </>,

  // Folder + network branch — for Samba/NAS share
  brandFolderNet: <>
    <path d="M3 7a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7z"/>
    <circle cx="9" cy="14" r="1.3"/>
    <circle cx="15" cy="11" r="1.3"/>
    <circle cx="15" cy="17" r="1.3"/>
    <line x1="10" y1="13.5" x2="14" y2="11.5"/>
    <line x1="10" y1="14.5" x2="14" y2="16.5"/>
  </>,

  // Terminal prompt — for SSH
  brandTerminal: <>
    <rect x="3" y="4" width="18" height="16" rx="1.5"/>
    <polyline points="7 9 10 12 7 15"/>
    <line x1="12" y1="15" x2="17" y2="15"/>
  </>,

  // Socket — Podman socket
  brandSocket: <>
    <circle cx="12" cy="12" r="9"/>
    <circle cx="12" cy="12" r="3"/>
    <line x1="12" y1="3" x2="12" y2="6"/>
    <line x1="12" y1="18" x2="12" y2="21"/>
    <line x1="3" y1="12" x2="6" y2="12"/>
    <line x1="18" y1="12" x2="21" y2="12"/>
  </>,

  // Heartbeat — Uptime Kuma stand-in
  brandHeartbeat: <>
    <polyline points="2 12 6 12 9 4 15 20 18 12 22 12"/>
  </>,

  /* Topology */
  router: <>
    <rect x="3" y="13" width="18" height="7" rx="1.5"/>
    <line x1="7" y1="16.5" x2="7" y2="16.51"/>
    <line x1="11" y1="16.5" x2="11" y2="16.51"/>
    <path d="M7 9.5a5 5 0 0 1 10 0"/>
    <path d="M4 7a8 8 0 0 1 16 0"/>
    <line x1="17" y1="13" x2="17" y2="10"/>
  </>,
  phone: <>
    <rect x="7" y="2.5" width="10" height="19" rx="2"/>
    <line x1="11.5" y1="18.5" x2="12.5" y2="18.5"/>
  </>,
  laptop: <>
    <rect x="4" y="5" width="16" height="10" rx="1"/>
    <path d="M2 18.5h20l-1.5-3H3.5z"/>
  </>,
  tv: <>
    <rect x="3" y="4" width="18" height="12" rx="1.5"/>
    <line x1="8" y1="20" x2="16" y2="20"/>
    <line x1="12" y1="16" x2="12" y2="20"/>
  </>,
  globe: <>
    <circle cx="12" cy="12" r="9"/>
    <line x1="3" y1="12" x2="21" y2="12"/>
    <path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>
  </>,
};
