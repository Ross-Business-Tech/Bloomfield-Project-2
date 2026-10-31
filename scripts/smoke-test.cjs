#!/usr/bin/env node
/**
 * smoke-test.cjs: open the built app (./dist) in a headless browser and check it actually works.
 *
 *   npm run build && node scripts/smoke-test.cjs
 *   SMOKE_PATHS="/,/about" node scripts/smoke-test.cjs     # check extra pages
 *
 * For each page, at desktop (1280x800) and phone (390x844) sizes, it fails on:
 *   JavaScript errors, console errors, failed or 4xx/5xx requests to the app itself,
 *   and blank pages. Screenshots go to smoke-results/ so Claude can look at them.
 * Third-party requests (fonts, CDNs, APIs) are reported as warnings only, because the cloud
 * sandbox may block them even though they work for real visitors.
 * Requests to /.herenow/* (Site Data, proxy routes) are ignored: they only exist on here.now.
 *
 * Exit: 0 = passed, or SKIPPED when no browser is installed; 1 = problems found.
 * Uses the Playwright + Chromium that the environment setup script installs (global npm),
 * falling back to a project-local playwright if present.
 */
'use strict';
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const DIST = path.resolve(ROOT, process.env.HERENOW_DIST_DIR || 'dist');
const OUT = path.join(ROOT, 'smoke-results');
const PAGES = (process.env.SMOKE_PATHS || '/').split(',').map((s) => s.trim()).filter(Boolean);
const PLATFORM_ONLY = /\/\.herenow\//;
const VIEWPORTS = [
  { name: 'desktop', viewport: { width: 1280, height: 800 } },
  { name: 'phone', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
];
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8',
};

if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/ms-playwright')) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/ms-playwright'; // where the setup script installs Chromium
}

function skip(why) {
  console.log(`SMOKE TEST SKIPPED: ${why}\n(The app was not checked in a browser. Publishing still works.)`);
  process.exit(0);
}

// Serve dist/ like here.now with SPA mode: real files as-is, unknown paths fall back to index.html.
function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      let urlPath = '/';
      try { urlPath = decodeURIComponent(new URL(req.url, 'http://x').pathname); } catch { /* keep / */ }
      if (PLATFORM_ONLY.test(urlPath)) { res.writeHead(404); res.end(); return; }
      let file = path.resolve(DIST, `.${urlPath}`);
      if (file !== DIST && !file.startsWith(DIST + path.sep)) { res.writeHead(403); res.end(); return; }
      if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
      if (!fs.existsSync(file)) file = path.join(DIST, 'index.html');
      res.writeHead(200, { 'content-type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream' });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

async function launchBrowser() {
  const candidates = [];
  try { candidates.push(path.join(execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim(), 'playwright')); } catch { /* no npm */ }
  try { candidates.push(require.resolve('playwright', { paths: [ROOT] })); } catch { /* not local */ }
  const errors = [];
  for (const c of candidates) {
    let pw;
    try { pw = require(c); } catch (e) { errors.push(`${c}: ${e.message.split('\n')[0]}`); continue; }
    try { return await pw.chromium.launch({ headless: true }); } catch (e) { errors.push(`${c}: ${e.message.split('\n')[0]}`); }
  }
  return skip(errors.length ? `no working browser (${errors.join('; ')})` : 'Playwright is not installed in this environment');
}

async function main() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) {
    console.error(`✖ ${path.relative(ROOT, DIST) || '.'}/index.html not found. Run "npm run build" first.`);
    process.exit(1);
  }
  const browser = await launchBrowser();
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}`;
  fs.mkdirSync(OUT, { recursive: true });

  // message -> set of "page (size)" where it was seen, so repeats collapse into one line
  const problems = new Map();
  const warnings = new Map();
  const add = (map, where, msg) => { if (!map.has(msg)) map.set(msg, new Set()); map.get(msg).add(where); };
  const shots = [];
  for (const pagePath of PAGES) {
    for (const vp of VIEWPORTS) {
      const where = `${pagePath} (${vp.name})`;
      const { name: _name, ...contextOptions } = vp;
      const context = await browser.newContext(contextOptions);
      const page = await context.newPage();
      const isOurs = (u) => u.startsWith(base) && !PLATFORM_ONLY.test(u);
      const thirdParty = (u) => add(warnings, where, `third-party resource did not load: ${u.split('?')[0]} ` +
        '(the sandbox may block it; check it on the published link)');
      page.on('pageerror', (e) => add(problems, where, `JavaScript error: ${e.message.split('\n')[0]}`));
      page.on('console', (m) => {
        if (m.type() !== 'error' || PLATFORM_ONLY.test(m.text())) return;
        const u = m.location()?.url || '';
        if (PLATFORM_ONLY.test(u)) return;
        if (u && !isOurs(u)) return thirdParty(u);
        add(problems, where, `console error: ${m.text().slice(0, 200)}`);
      });
      page.on('requestfailed', (r) => {
        if (PLATFORM_ONLY.test(r.url())) return;
        if (!isOurs(r.url())) return thirdParty(r.url());
        add(problems, where, `could not load ${r.url().replace(base, '')} (${r.failure()?.errorText || 'failed'})`);
      });
      page.on('response', (r) => {
        if (r.status() < 400 || PLATFORM_ONLY.test(r.url())) return;
        if (!isOurs(r.url())) return thirdParty(r.url());
        add(problems, where, `HTTP ${r.status()} for ${r.url().replace(base, '')}`);
      });

      try {
        await page.goto(base + pagePath, { waitUntil: 'networkidle', timeout: 30000 });
      } catch (e) {
        add(problems, where, `page did not finish loading (${e.message.split('\n')[0]})`);
      }
      await page.waitForTimeout(500);
      // Mobile emulation widens window.innerWidth to fit oversized content, so compare against
      // the configured screen width instead.
      const seen = await page.evaluate((screenWidth) => ({
        text: (document.body?.innerText || '').trim().length,
        media: document.querySelectorAll('canvas, svg, img, video').length,
        overflowX: document.documentElement.scrollWidth > screenWidth + 1,
      }), vp.viewport.width).catch(() => ({ text: 0, media: 0, overflowX: false }));
      if (!seen.text && !seen.media) add(problems, where, 'the page is blank (no visible text or images)');
      if (seen.overflowX && vp.name === 'phone') add(warnings, where, 'content is wider than a phone screen (sideways scrolling)');

      const file = path.join(OUT, `${pagePath.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'home'}-${vp.name}.png`);
      await page.screenshot({ path: file, fullPage: true }).catch(() => {});
      shots.push(path.relative(ROOT, file));
      await context.close();
    }
  }
  await browser.close();
  server.close();

  const fmt = (mark, map) => [...map].map(([msg, where]) => `  ${mark} ${msg}\n      on: ${[...where].join(', ')}`);
  const lines = [
    problems.size ? `✖ SMOKE TEST FAILED (${problems.size} problem${problems.size > 1 ? 's' : ''})` : '✔ SMOKE TEST PASSED',
    ...fmt('✖', problems),
    ...fmt('⚠', warnings),
    `  Screenshots: ${shots.join(', ')}`,
  ];
  fs.writeFileSync(path.join(OUT, 'report.txt'), `${lines.join('\n')}\n`);
  console.log(lines.join('\n'));
  process.exit(problems.size ? 1 : 0);
}

main().catch((e) => { console.error(`✖ smoke test crashed: ${e.stack || e.message}`); process.exit(1); });
