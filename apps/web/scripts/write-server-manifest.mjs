import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL("..", import.meta.url));
const output = join(here, "../../src/drover/server/web/static/client");

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml"
};

async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  return (await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  }))).flat();
}

const entries = {};
for (const path of await files(output)) {
  const name = relative(output, path).replaceAll("\\", "/");
  if (name === "client-manifest.json" || name.startsWith(".vite/")) continue;
  const extension = name.slice(name.lastIndexOf("."));
  const contentType = contentTypes[extension];
  if (!contentType) throw new Error(`unsupported client asset: ${name}`);
  entries[name] = {
    content_type: contentType,
    cache_control: name === "index.html" ? "no-store" : "public, max-age=31536000, immutable"
  };
}

if (!entries["index.html"]) throw new Error("client build has no index.html");
await writeFile(join(output, "client-manifest.json"), `${JSON.stringify(entries, null, 2)}\n`);
