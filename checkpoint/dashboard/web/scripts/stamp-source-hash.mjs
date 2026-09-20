// Record what the committed bundle was built from.
//
// The bundle in ../static is committed so `pip install` from git gives a
// working dashboard with no Node. That only helps if it matches the source,
// and the obvious check — rebuild in CI and diff the output — does not work:
// the bundler's output is not byte-identical across operating systems and Node
// majors, so a correct bundle built on one machine fails the check on another.
//
// What actually has to be true is narrower: nobody changed the source and
// forgot to rebuild. So the build stamps a hash of its *inputs* beside the
// output, and CI recomputes that hash and compares. It is stable everywhere,
// and it fails for exactly the reason we care about.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const web = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(web, "../static/.source-hash");

// Everything the bundle's contents depend on. package-lock.json is in here
// because a dependency bump changes the output without touching src/.
const INPUTS = ["src", "index.html", "package.json", "package-lock.json",
                "vite.config.ts", "tsconfig.json", "tsconfig.app.json",
                "postcss.config.js", "tailwind.config.js"];

function files(path) {
  let stat;
  try {
    stat = statSync(path);
  } catch {
    return []; // an optional config this project does not use
  }
  if (!stat.isDirectory()) return [path];
  return readdirSync(path)
    .sort()
    .flatMap((entry) => files(join(path, entry)));
}

const hash = createHash("sha256");
for (const input of INPUTS) {
  for (const file of files(resolve(web, input))) {
    // The path goes in too, so renaming a file counts as a change.
    hash.update(relative(web, file).split("\\").join("/"));
    hash.update(readFileSync(file));
  }
}
writeFileSync(out, `${hash.digest("hex")}\n`);
console.log(`stamped ${relative(web, out).split("\\").join("/")}`);
