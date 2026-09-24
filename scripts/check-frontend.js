#!/usr/bin/env node
/* Parse every frontend source file with the same Babel parser Vite uses.
 *
 * This exists because a syntax error shipped straight to the browser and took
 * the whole dashboard down with a wall of stack trace. The check that was in
 * place counted brackets, which is not a syntax check: `await` inside a nested
 * non-async arrow function has perfectly balanced brackets and is still fatal.
 *
 *   node scripts/check-frontend.js
 *
 * Exits non-zero if anything fails to parse.
 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', 'frontend');
const SRC = path.join(ROOT, 'src');

let parser;
try {
  parser = require('@babel/parser');
} catch {
  try {
    const found = execSync(
      `find ${ROOT}/node_modules -maxdepth 7 -path '*@babel/parser/lib/index.js' 2>/dev/null | head -1`
    ).toString().trim();
    if (!found) throw new Error('not found');
    parser = require(found);
  } catch {
    console.log('@babel/parser not available — skipping (run pnpm install first)');
    process.exit(0);
  }
}

let bad = 0, n = 0;
(function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const f = path.join(dir, e.name);
    if (e.isDirectory()) { walk(f); continue; }
    if (!/\.(jsx?|mjs)$/.test(e.name)) continue;
    n++;
    try {
      parser.parse(fs.readFileSync(f, 'utf8'), { sourceType: 'module', plugins: ['jsx'] });
    } catch (err) {
      bad++;
      const where = err.loc ? `${err.loc.line}:${err.loc.column}` : '?';
      console.log(`SYNTAX ERROR  ${path.relative(SRC, f)}:${where}  ${err.message.split('. (')[0]}`);
    }
  }
})(SRC);

console.log(bad ? `${bad} of ${n} files broken` : `frontend: all ${n} files parse cleanly`);
process.exit(bad ? 1 : 0);
