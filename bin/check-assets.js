#!/usr/bin/env node
/**
 * bin/check-assets.js — is the CURRENT ui/ actually inside the binary?
 *
 * ============================================================================
 * ⚠️⚠️ WHY THIS EXISTS
 * ============================================================================
 * Tauri embeds the interface INTO THE BINARY at compile time (frontendDist = "../ui",
 * brotli-compressed into a cache under target/). ⚠️ AND CARGO DOES NOT TRACK ui/*.js AS AN
 * INPUT TO THAT CODEGEN.
 *
 * So `cargo build` reports "Finished" while re-embedding nothing. Measured:
 *
 *     current ui/setup.js       18,987 bytes   contains the new code
 *     newest embedded asset      4,089 bytes   contains none of it
 *
 * Every fix had been written, committed, unit-tested and rebuilt, and the app kept serving
 * an interface from hours earlier.
 *
 * ⚠️ THAT IS WORSE THAN THE `open`-DOES-NOT-RELAUNCH TRAP, which at least showed a stale
 * BINARY. This shows a CURRENT binary containing a STALE INTERFACE, so nothing about the
 * build's freshness can tell you the interface is old. The only way to know is to look.
 *
 * ============================================================================
 * ⚠️ WHY THIS COMPARES CONTENT AND NOT FILENAMES
 * ============================================================================
 * The first version assumed Tauri names each asset by the SHA-256 of its contents. It does
 * not — it reported all four files missing while a correct copy of each was sitting in the
 * cache. **A checker that cries wolf is worse than no checker**, so the comparison is now
 * made against the decompressed BYTES, which is the thing that actually matters.
 *
 * Exit 0 = every ui file is embedded verbatim. Exit 1 = at least one is stale or missing.
 */

const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

const ROOT = path.resolve(__dirname, "..");
const UI = path.join(ROOT, "ui");
const BUILD = path.join(ROOT, "src-tauri/target/release/build");

// ⚠⚠️ EVERY FILE THE APP LOADS, AND THIS LIST HAS TO BE UPDATED WHEN ONE IS ADDED.
//
// ai.js was left out when it was created, so the checker reported "all embedded" while
// silently not looking at the file that renders the AI step — the newest and most
// changed part of the interface. **A checker with an incomplete list reports SUCCESS for
// the files it knows about, which is indistinguishable from reporting success for all of
// them.** The whole point of this script is to catch a stale binary, and it would have
// missed the most likely one.
const WATCH = ["index.html", "app.js", "setup.js", "ai.js", "style.css"];

function embeddedTexts() {
  const out = [];
  if (!fs.existsSync(BUILD)) return out;
  for (const d of fs.readdirSync(BUILD)) {
    if (!d.startsWith("device-search-")) continue;
    const dir = path.join(BUILD, d, "out", "tauri-codegen-assets");
    if (!fs.existsSync(dir)) continue;
    for (const f of fs.readdirSync(dir)) {
      const raw = fs.readFileSync(path.join(dir, f));
      // ⚠️ brotli first, plain text second. An uncompressed asset would otherwise be
      // silently counted as absent, which is the same false-negative the filename
      // version produced.
      try {
        out.push({ name: f, text: zlib.brotliDecompressSync(raw).toString("utf8") });
      } catch (e) {
        out.push({ name: f, text: raw.toString("utf8") });
      }
    }
  }
  return out;
}

function main() {
  const assets = embeddedTexts();
  if (!assets.length) {
    console.log("  ✗ no tauri-codegen-assets found — has the app ever been built?");
    return 1;
  }

  const missing = [];
  for (const name of WATCH) {
    const p = path.join(UI, name);
    if (!fs.existsSync(p)) {
      console.log(`  ✗ ui/${name} does not exist`);
      missing.push(name);
      continue;
    }
    const want = fs.readFileSync(p, "utf8");

    // ⚠️⚠️ HTML IS COMPARED BY WHAT IT CONTAINS, NOT BY ITS BYTES.
    //
    // Tauri runs the HTML through a processor on the way in, and chasing its transformations
    // is a losing game. Measured, for the SAME document, in three steps:
    //     "<!DOCTYPE html>\n<html ...>\n<head>"  ->  whitespace between tags, removed
    //     "<meta charset=\"utf-8\" />"            ->  "<meta charset=\"utf-8\">"   (void element)
    //     "<circle cx=.. r=.. />"               ->  "<circle ..></circle>"       (SVG EXPANDED)
    //
    // Each fix to this checker matched one more transformation and left the next, and a
    // checker that reports a correct build as broken is worse than none — it was the third
    // false negative from this file.
    //
    // ⚠️ SO IT COMPARES THE ELEMENT IDS. Every id in the file on disk must exist in the copy
    // in the binary. That is insensitive to reformatting and completely sensitive to a stale
    // version, because a stale version is missing the ids that were added since.
    //
    // ⚠️ JS AND CSS ARE STILL COMPARED EXACTLY, because Tauri does not touch them — they
    // matched byte for byte throughout.
    const isHtml = name.endsWith(".html");
    const idsOf = (t) => [...t.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]).sort().join(",");
    const hit = isHtml
      ? (() => {
          const wantIds = idsOf(want).split(",").filter(Boolean);
          return assets.find((a) => {
            const got = new Set(idsOf(a.text).split(",").filter(Boolean));
            return wantIds.length > 0 && wantIds.every((id) => got.has(id));
          });
        })()
      : assets.find((a) => a.text === want);

    if (hit) {
      console.log(`  ✓ ui/${name.padEnd(12)} embedded (${want.length.toLocaleString()} bytes)`);
    } else {
      const detail = isHtml
        ? " — the embedded page is missing elements this one defines"
        : (() => {
            const near = assets.filter((a) => a.text.startsWith(want.slice(0, 24)))
                               .sort((a, b) => b.text.length - a.text.length)[0];
            return near ? ` — newest embedded copy is ${near.text.length.toLocaleString()} bytes`
                        : " — no copy of this file is embedded at all";
          })();
      console.log(`  ✗ ui/${name.padEnd(12)} NOT embedded${detail}`);
      missing.push(name);
    }
  }

  if (missing.length) {
    console.log();
    console.log("  ⚠️ the binary does NOT contain the current interface.");
    console.log("     `cargo build` does not treat ui/* as an input to the Tauri codegen, so");
    console.log("     changing only the interface rebuilds nothing. Touch a Rust file:");
    console.log("         touch src-tauri/src/main.rs && ./bin/make-app.sh");
    return 1;
  }
  return 0;
}

process.exit(main());
