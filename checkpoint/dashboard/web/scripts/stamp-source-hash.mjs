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
// output, and `tests/test_spa_bundle.py` recomputes that hash and compares. It
// is stable everywhere, and it fails for exactly the reason we care about.
//
// "Stable everywhere" is the whole point, so the hash must not depend on how
// the checkout landed on disk. `.gitattributes` keeps these files LF in git,
// but the conversion happens on commit, not on save: a Windows editor can
// leave CRLF in the working tree and git still reports it clean. Hashing raw
// bytes there produces a stamp no Linux runner can reproduce, so text is
// normalised to LF before it goes in.
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

function contents(file) {
  const raw = readFileSync(file);
  if (raw.includes(0)) return raw; // binary: leave it exactly as it is
  return Buffer.from(raw.toString("utf8").split("\r\n").join("\n"), "utf8");
}

const hash = createHash("sha256");
for (const input of INPUTS) {
  for (const file of files(resolve(web, input))) {
    // The path goes in too, so renaming a file counts as a change.
    hash.update(relative(web, file).split("\\").join("/"));
    hash.update(contents(file));
  }
}
writeFileSync(out, `${hash.digest("hex")}\n`);
console.log(`stamped ${relative(web, out).split("\\").join("/")}`);
