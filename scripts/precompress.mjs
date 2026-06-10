/* Precompress dist assets at build time so the Pi's Flask sidecar can serve
   ready-made .gz files (see send_dist in server/app.py) instead of compressing
   large bundles per request. */
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const DIST = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");
const COMPRESSIBLE = new Set([".js", ".css", ".html", ".svg", ".json", ".map", ".txt", ".webmanifest"]);
const MIN_BYTES = 1024;

async function* walk(dir) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else yield full;
  }
}

let count = 0;
let saved = 0;
for await (const file of walk(DIST)) {
  if (!COMPRESSIBLE.has(path.extname(file)) || file.endsWith(".gz")) continue;
  const data = await fs.readFile(file);
  if (data.length < MIN_BYTES) continue;
  const gz = gzipSync(data, { level: 9 });
  if (gz.length >= data.length) continue;
  await fs.writeFile(`${file}.gz`, gz);
  count += 1;
  saved += data.length - gz.length;
}
console.log(`precompress: wrote ${count} .gz files, saved ${(saved / 1024).toFixed(0)} KB`);
