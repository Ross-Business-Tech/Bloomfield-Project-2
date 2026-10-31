#!/usr/bin/env node
/**
 * herenow-publish.mjs: build the app and publish ./dist to here.now
 *
 *   node scripts/herenow-publish.mjs preview      # from a Claude Code session (test link)
 *   node scripts/herenow-publish.mjs pr-preview   # GitHub Actions, on pull requests
 *   node scripts/herenow-publish.mjs production   # GitHub Actions, on main only
 *
 * preview     No API key -> anonymous site, expires in 24 h (claim link printed once).
 *             API key    -> your here.now account, expires 7 days after last publish.
 *             Re-running updates the same link (state kept in .herenow-preview.json, gitignored).
 * pr-preview  Team key, expires 7 days after last publish. Refuses to run outside CI.
 * production  Team key, permanent, into the workspace (main branch). Refuses to run outside CI.
 *             HERENOW_COMING_SOON=1 publishes a "Coming soon" page (team name only) instead of the app.
 * development Same key, permanent, OUTSIDE the workspace (the key owner's personal account), so the
 *             site keeps an unguessable address (development branch). Refuses to run outside CI.
 *
 * Environment
 *   HERENOW_API_KEY         API key (else ~/.herenow/credentials). Required for CI modes.
 *   HERENOW_AUTH_VIA_PROXY  "1" = key is injected by a network secret on the Claude environment
 *                           (personal environments only); treat requests as authenticated.
 *   HERENOW_WORKSPACE       here.now workspace, e.g. rossbusinesstech (sent as X-HereNow-Account on
 *                           authenticated requests). In preview mode it requires a signed-in student:
 *                           see scripts/herenow-login.mjs.
 *   HERENOW_PROD_SLUG       production site slug (repo variable).
 *   HERENOW_DEV_SLUG        development site slug (repo variable).
 *   HERENOW_COMING_SOON     "1" (production only): publish the Coming soon page, not the build.
 *   HERENOW_TEAM_NAME       team name shown on the Coming soon page.
 *   HERENOW_SLUG            explicit slug to update (pr-preview uses this).
 *   HERENOW_ACCESS          applied when a site is CREATED: public | members | <domain>[,<domain>]
 *                           (default: public when HERENOW_WORKSPACE is set, otherwise untouched)
 *   HERENOW_LABEL           workspace label for a NEW site, e.g. the repo name, giving
 *                           https://<label>.<workspace>.here.now/. Sent on finalize; a taken label
 *                           gets -2 ... -5, then here.now generates one.
 *   HERENOW_FOLDER          dashboard folder (default: package name). Previews go in "<folder> previews".
 *   HERENOW_DIST_DIR        build output dir (default: dist)
 *   HERENOW_SPA             "0" disables SPA fallback routing (default on)
 *   HERENOW_SKIP_BUILD      "1" skips `npm run build`
 *   HERENOW_API_BASE        API base URL (default https://here.now; used by admin/tests)
 *
 * Zero dependencies. All HTTP goes through curl so it honours the cloud sandbox's proxy
 * settings (Node's built-in fetch does not read HTTPS_PROXY by default).
 * API reference: https://here.now/docs
 */
import { execFile, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileP = promisify(execFile);
const env = process.env;
const MODES = ['preview', 'pr-preview', 'production', 'development'];
const MODE = process.argv[2] || 'preview';
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const API = (env.HERENOW_API_BASE || 'https://here.now').replace(/\/+$/, '');
const API_ORIGIN = new URL(API).origin;
let DIST = path.resolve(ROOT, env.HERENOW_DIST_DIR || 'dist'); // replaced by a temp dir for Coming soon
const STATE_FILE = path.join(ROOT, '.herenow-preview.json');
const IN_CI = env.GITHUB_ACTIONS === 'true';
const CLIENT = env.HERENOW_CLIENT || `${IN_CI ? 'github-actions' : 'claude-code'}/ross-herenow-publish`;
// development sites never go into the workspace: outside it they keep an unguessable slug address.
const WORKSPACE = MODE === 'development' ? '' : (env.HERENOW_WORKSPACE || '').trim().toLowerCase() // accepts "name" or "name.here.now"
  .replace(/^https?:\/\//, '').replace(/\.here\.now\/?$/, '');
const PREVIEW_TTL = 7 * 24 * 60 * 60;
const MAX_FILES = 2500;

const log = (...a) => console.error(...a); // progress -> stderr, final summary -> stdout
function die(msg) {
  console.error(`\n✖ here.now publish stopped: ${msg}\n`);
  process.exit(1);
}

// ---------- credentials ----------
let KEY = (env.HERENOW_API_KEY || '').trim();
if (!KEY) {
  try { KEY = fs.readFileSync(path.join(os.homedir(), '.herenow', 'credentials'), 'utf8').trim(); } catch { /* none */ }
}
const AUTH_VIA_PROXY = env.HERENOW_AUTH_VIA_PROXY === '1';
const AUTHED = Boolean(KEY) || AUTH_VIA_PROXY;

// The key goes to curl through a 0600 header file, never on the command line (visible in ps),
// and only to the API origin (never to presigned storage URLs).
let authHeaderFile = '';
function authArgs(url) {
  if (!KEY || new URL(url).origin !== API_ORIGIN) return [];
  if (!authHeaderFile) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'herenow-'));
    authHeaderFile = path.join(dir, 'auth');
    fs.writeFileSync(authHeaderFile, `Authorization: Bearer ${KEY}\n`, { mode: 0o600 });
    process.on('exit', () => fs.rmSync(dir, { recursive: true, force: true }));
  }
  return ['-H', `@${authHeaderFile}`];
}

// ---------- helpers ----------
function api(method, urlOrPath, payload) {
  const url = /^https?:\/\//.test(urlOrPath) ? urlOrPath : API + urlOrPath;
  const args = ['-sS', '-X', method, url, '--max-time', '120',
    '-H', `X-HereNow-Client: ${CLIENT}`, '-H', 'accept: application/json',
    '-w', '\n%{http_code}', ...authArgs(url)];
  // Anonymous publishes have no account, so they can never target a workspace.
  if (WORKSPACE && AUTHED && new URL(url).origin === API_ORIGIN) args.push('-H', `X-HereNow-Account: ${WORKSPACE}`);
  let input;
  if (payload !== undefined) {
    args.push('-H', 'content-type: application/json', '--data-binary', '@-'); // body via stdin: no arg-length limit
    input = JSON.stringify(payload);
  }
  try {
    const out = execFileSync('curl', args, { input, encoding: 'utf8', maxBuffer: 64 << 20, stdio: ['pipe', 'pipe', 'pipe'] });
    const cut = out.lastIndexOf('\n');
    const text = out.slice(0, cut);
    let json = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: Number(out.slice(cut + 1)), json, text };
  } catch (e) {
    return { status: 0, json: null, text: String(e.stderr || e.message || '').trim() };
  }
}

const ok = (r) => r.status >= 200 && r.status < 300;

// Sandbox egress proxies answer blocked hosts with 403 + a plain-text body (e.g. "Host not in
// allowlist: here.now"); here.now's own API always answers JSON. Use that to tell them apart.
const proxyBlocked = (status, body) => status === 403 && /allow|not permitted|denied by policy/i.test(body || '') && !/^\s*[{<]/.test(body || '');

function explain(res, what) {
  if (res.status === 403 && !res.json && proxyBlocked(res.status, res.text)) {
    return `${what}: the network blocked ${API}.\n  Proxy said: ${res.text.trim().slice(0, 160)}\n` +
      "  Add here.now to the Claude environment's Allowed domains (see admin/environment/allowed-domains.txt).";
  }
  if (res.status === 0) {
    return `${what}: could not reach ${API}.\n  In Claude Code on the web this almost always means here.now is not in the ` +
      `environment's allowed domains (see admin/environment/allowed-domains.txt).\n  curl said: ${res.text.split('\n').pop()}`;
  }
  const j = res.json || {};
  const detail = [j.code && `(${j.code})`, j.error, j.message && j.message !== j.error ? j.message : '']
    .filter(Boolean).join(' ') || res.text.slice(0, 300);
  const hint = {
    401: 'The here.now API key is missing or invalid.',
    403: 'This key may not change that site. On a workspace site only the original publisher or a workspace admin can update it.',
    429: `Rate limited${j.retry_after ? `; retry in about ${j.retry_after}s` : ''}. Anonymous publishing is limited per IP address; an API key raises the limit.`,
  }[res.status];
  const wsHint = WORKSPACE && /account|workspace/.test(String(j.code || ''))
    ? `\n  Is this here.now account a member of the "${WORKSPACE}" workspace? Check: node scripts/herenow-login.mjs status`
    : '';
  return `${what} -> HTTP ${res.status} ${detail}${hint ? `\n  ${hint}` : ''}${wsHint}`;
}

const readJson = (p) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } };
const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');
const sleep = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

function gitBranch() {
  try { return execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch { return ''; }
}

const TYPES = {
  html: 'text/html; charset=utf-8', htm: 'text/html; charset=utf-8', css: 'text/css; charset=utf-8',
  js: 'text/javascript; charset=utf-8', mjs: 'text/javascript; charset=utf-8', cjs: 'text/javascript; charset=utf-8',
  json: 'application/json; charset=utf-8', map: 'application/json; charset=utf-8', webmanifest: 'application/manifest+json',
  txt: 'text/plain; charset=utf-8', md: 'text/markdown; charset=utf-8', csv: 'text/csv; charset=utf-8', xml: 'application/xml',
  svg: 'image/svg+xml', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
  avif: 'image/avif', ico: 'image/x-icon', woff: 'font/woff', woff2: 'font/woff2', ttf: 'font/ttf', otf: 'font/otf',
  pdf: 'application/pdf', wasm: 'application/wasm', mp4: 'video/mp4', webm: 'video/webm', mp3: 'audio/mpeg',
  wav: 'audio/wav', ogg: 'audio/ogg', zip: 'application/zip',
};
const contentType = (p) => TYPES[path.extname(p).slice(1).toLowerCase()] || 'application/octet-stream';
const TEXTY = /\.(html?|css|m?js|cjs|json|map|txt|md|svg|xml|csv|webmanifest)$/i;

// Keys that must never ship in a public website. (Browser-safe keys such as Google/Firebase
// "AIza..." web keys and Stripe "pk_" publishable keys are intentionally NOT flagged.)
const SECRET_PATTERNS = [
  ['here.now API key', /\bhnk_[0-9a-f]{32,}/],
  ['AI/API secret key (sk-...)', /\bsk-(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}/],
  ['Stripe secret key', /\b[rs]k_live_[0-9A-Za-z]{20,}/],
  ['AWS access key', /\bAKIA[0-9A-Z]{16}\b/],
  ['GitHub token', /\bgh[pousr]_[A-Za-z0-9]{36,}/],
];

function walk(dir, prefix = '') {
  const out = [];
  for (const d of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = prefix ? `${prefix}/${d.name}` : d.name;
    if (d.isDirectory()) out.push(...walk(path.join(dir, d.name), rel));
    else if (d.isFile() && d.name !== '.DS_Store') out.push(rel);
  }
  return out;
}

async function pool(items, size, fn) {
  let next = 0;
  const worker = async () => { while (next < items.length) await fn(items[next++]); };
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, worker));
}

// Static "Coming soon" page: team name only, no external resources, not indexed.
function comingSoonPage(name) {
  const esc = String(name).slice(0, 80).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>${esc} | Coming soon</title>
<style>
html,body{height:100%;margin:0}body{display:flex;align-items:center;justify-content:center;background:#00274c;color:#fff;
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;text-align:center;padding:24px;box-sizing:border-box}
h1{font-size:clamp(2rem,7vw,4rem);margin:0 0 .4em;line-height:1.1}p{margin:0;font-size:clamp(1rem,3.5vw,1.5rem);color:#ffcb05;
letter-spacing:.2em;text-transform:uppercase}
</style></head>
<body><main><h1>${esc}</h1><p>Coming soon</p></main></body></html>
`;
}

// ---------- main ----------
async function main() {
  if (!MODES.includes(MODE)) die(`unknown mode "${MODE}". Use one of: ${MODES.join(', ')}.`);
  if (MODE !== 'preview' && !IN_CI && env.HERENOW_ALLOW_LOCAL !== '1') {
    die(`"${MODE}" runs only in GitHub Actions. From a Claude session, publish a test link with: npm run publish:preview`);
  }
  if (MODE !== 'preview' && !AUTHED) die('HERENOW_API_KEY is not set (add it as a GitHub Actions repository secret).');
  if (MODE === 'preview' && WORKSPACE && !AUTHED) {
    die(`this environment publishes previews into the "${WORKSPACE}" here.now workspace, which needs the student\n` +
      '  to be signed in. Ask which email they use with here.now, then run:\n' +
      '    node scripts/herenow-login.mjs start <email>        (here.now emails them a code)\n' +
      '    node scripts/herenow-login.mjs finish <email> <code>\n' +
      '  If they cannot sign in right now, `HERENOW_WORKSPACE= npm run publish:preview` makes an anonymous\n' +
      '  24-hour link outside the workspace; tell them that is what it is.');
  }

  const pkg = readJson(path.join(ROOT, 'package.json')) || {};
  const appName = (env.HERENOW_APP_NAME || pkg.name || path.basename(ROOT)).slice(0, 50);

  // 1. Build (or generate the Coming soon page, which needs no project code at all)
  const comingSoon = env.HERENOW_COMING_SOON === '1';
  if (comingSoon && MODE !== 'production') die('HERENOW_COMING_SOON only applies to production.');
  if (comingSoon) {
    DIST = fs.mkdtempSync(path.join(os.tmpdir(), 'herenow-soon-'));
    fs.writeFileSync(path.join(DIST, 'index.html'), comingSoonPage(env.HERENOW_TEAM_NAME || appName));
    log('→ Publishing the Coming soon page instead of the app');
  } else if (env.HERENOW_SKIP_BUILD !== '1' && pkg.scripts?.build) {
    log('→ Building (npm run build)…');
    // The build runs project code and npm packages, so it never gets the here.now key.
    const buildEnv = { ...process.env };
    delete buildEnv.HERENOW_API_KEY;
    try { execFileSync('npm', ['run', 'build'], { cwd: ROOT, stdio: ['ignore', 2, 2], env: buildEnv }); } catch {
      die('the build failed. Fix the errors above, then publish again.');
    }
  }
  const distRel = path.relative(ROOT, DIST) || '.';
  if (!fs.existsSync(path.join(DIST, 'index.html'))) die(`${distRel}/index.html not found, so there is no website to publish.`);

  // 2. Manifest + safety checks
  const paths = walk(DIST);
  if (paths.length > MAX_FILES) die(`${paths.length} files in ${distRel}/; here.now allows ${MAX_FILES} per site.`);
  const files = [];
  for (const p of paths) {
    const buf = fs.readFileSync(path.join(DIST, p));
    if (TEXTY.test(p) && buf.length < 10 << 20) {
      const text = buf.toString('utf8');
      for (const [label, re] of SECRET_PATTERNS) {
        const m = text.match(re);
        if (m) {
          die(`${distRel}/${p} contains what looks like a ${label} (${m[0].slice(0, 8)}…).\n` +
            '  Anything in a published website is public. Remove the key from the code and call that\n' +
            '  service through a here.now proxy route instead (see CLAUDE.md, "Calling paid APIs").');
        }
      }
    }
    files.push({ path: p, size: buf.length, contentType: contentType(p), hash: sha256(buf) });
  }
  if (!comingSoon && fs.existsSync(path.join(ROOT, 'public', '.herenow')) && !paths.some((p) => p.startsWith('.herenow/'))) {
    log(`  ⚠ public/.herenow/ exists but nothing from it reached ${distRel}/. Site Data / proxy manifests will be missing.`);
  }
  const usesPlatform = paths.filter((p) => p === '.herenow/data.json' || p === '.herenow/proxy.json');
  if (usesPlatform.length && !AUTHED) {
    log(`  ⚠ ${usesPlatform.join(' and ')} only work on account-owned sites. On this anonymous preview,\n` +
      '    saving data and proxy routes will not work. They will work on PR previews and production.');
  }

  // 3. Create or update?
  const state = MODE === 'preview' ? (readJson(STATE_FILE) || {}) : {};
  let slug = (env.HERENOW_SLUG || '').trim();
  if (MODE === 'production') {
    slug = (env.HERENOW_PROD_SLUG || '').trim();
    if (!slug && env.GITHUB_EVENT_NAME !== 'workflow_dispatch' && env.HERENOW_ALLOW_LOCAL !== '1') {
      die('no production site is configured yet. Run this workflow once by hand (GitHub > Actions > "here.now" >\n' +
        '  Run workflow, on main) to create it, then save the printed slug as repository variable HERENOW_PROD_SLUG.');
    }
  } else if (MODE === 'development') {
    slug = (env.HERENOW_DEV_SLUG || '').trim();
    if (!slug && env.GITHUB_EVENT_NAME !== 'workflow_dispatch' && env.HERENOW_ALLOW_LOCAL !== '1') {
      die('no development site is configured yet. Run this workflow once by hand on the development branch\n' +
        '  to create it, then save the printed slug as repository variable HERENOW_DEV_SLUG.');
    }
  } else if (MODE === 'preview' && !slug && state.api === API && state.authed === AUTHED) {
    slug = state.slug || '';
  }

  const branch = env.GITHUB_HEAD_REF || gitBranch();
  const displayName = (MODE === 'production' ? appName
    : MODE === 'development' ? `${appName} · development`
      : MODE === 'pr-preview' ? `${appName} · PR #${env.HERENOW_PR_NUMBER || '?'}`
        : `${appName} · preview${branch && branch !== 'HEAD' ? ` (${branch})` : ''}`).slice(0, 80);

  const body = { files, spaMode: env.HERENOW_SPA !== '0', displayName };
  if (AUTHED) {
    if (MODE === 'preview' || MODE === 'pr-preview') body.ttlSeconds = PREVIEW_TTL;
    const folder = (env.HERENOW_FOLDER || appName).slice(0, 48);
    body.folder = MODE === 'production' ? folder : MODE === 'development' ? `${folder} dev` : `${folder} previews`;
  }

  let res;
  let created = false;
  if (slug) {
    log(`→ Updating ${slug}…`);
    const update = { ...body };
    if (!AUTHED && state.claimToken) update.claimToken = state.claimToken;
    res = api('PUT', `/api/v1/publish/${encodeURIComponent(slug)}`, update);
    if ((res.status === 404 || res.status === 410) && MODE !== 'production') {
      log('  that site has expired or was deleted; creating a new one');
      slug = '';
    } else if (!ok(res)) {
      die(explain(res, `Updating ${slug}`));
    }
  }
  if (!slug) {
    log('→ Creating a new here.now site…');
    res = api('POST', '/api/v1/publish', body);
    if (!ok(res)) die(explain(res, 'Creating the site'));
    created = true;
  }
  const site = res.json || {};
  slug = site.slug || slug;
  const up = site.upload;
  if (!up?.versionId || !up?.finalizeUrl) die(`unexpected response from here.now: ${res.text.slice(0, 300)}`);

  // 4. Upload changed files straight to storage (presigned URLs; no auth header)
  const uploads = up.uploads || [];
  log(`→ Uploading ${uploads.length} file(s); ${(up.skipped || []).length} unchanged…`);
  await pool(uploads, 6, async (u) => {
    if (!paths.includes(u.path)) throw new Error(`here.now asked for an unknown file: ${u.path}`);
    const args = ['-sS', '-X', u.method || 'PUT', u.url, '--upload-file', path.join(DIST, u.path),
      '-H', 'Expect:', '-w', '\n%{http_code}', '--max-time', '600'];
    for (const [k, v] of Object.entries(u.headers || {})) args.push('-H', `${k}: ${v}`);
    let code = 0;
    let body = '';
    try {
      const out = (await execFileP('curl', args, { maxBuffer: 8 << 20 })).stdout;
      const cut = out.lastIndexOf('\n');
      code = Number(out.slice(cut + 1));
      body = out.slice(0, cut);
    } catch (e) { body = String(e.stderr || e.message).trim(); }
    if (code < 200 || code >= 300) {
      let host = '?';
      try { host = new URL(u.url).host; } catch { /* keep ? */ }
      const why = proxyBlocked(code, body) ? `the network blocked ${host}`
        : code ? `HTTP ${code} from ${host}: ${body.replace(/\s+/g, ' ').slice(0, 160)}` : body;
      throw new Error(`uploading ${u.path} failed (${why}).\n  File uploads go straight to storage at ${host}. ` +
        "In Claude Code on the web, *.r2.cloudflarestorage.com must be in the environment's Allowed domains " +
        '(it is in the default list, so tick "Also include default list").');
    }
  }).catch((e) => die(e.message));

  // 5. Finalize (the site is not live until this succeeds)
  log('→ Finalizing…');
  // here.now takes the workspace label on FINALIZE (openapi: FinalizePublishRequest.workspaceLabel),
  // not on create. Only for a newly created workspace site, so updates never rename or re-suffix it.
  // Explicit labels are strict (a taken label returns 409), so try label, label-2 ... label-5, then
  // let here.now generate one rather than leave the site unpublished.
  const label = (env.HERENOW_LABEL || '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  const tries = created && WORKSPACE && AUTHED && label ? [label, ...[2, 3, 4, 5].map((n) => `${label}-${n}`), null] : [null];
  let fin;
  for (const l of tries) {
    fin = api('POST', up.finalizeUrl, l ? { versionId: up.versionId, workspaceLabel: l } : { versionId: up.versionId });
    const taken = fin.status === 409 && /label/i.test(`${fin.json?.code || ''} ${fin.json?.error || ''}`);
    if (!taken) break;
    log(`  workspace label "${l}" is already taken; trying the next option`);
  }
  if (!ok(fin) || fin.json?.success === false) die(explain(fin, 'Finalizing'));
  const f = fin.json || {};
  for (const w of f.warnings || []) log(`  ⚠ here.now warning: ${typeof w === 'string' ? w : JSON.stringify(w)}`);

  // 6. Access mode on newly created account-owned sites (workspace sites default to members-only)
  const access = (env.HERENOW_ACCESS || (WORKSPACE ? 'public' : '')).trim();
  if (created && AUTHED && access) {
    const payload = access === 'public' ? { mode: 'anyone_with_link' }
      : access === 'members' ? { mode: 'account_members' }
        : { mode: 'restricted', allowedEmails: [], allowedDomains: access.split(',').map((s) => s.trim()).filter(Boolean), notify: false };
    const acc = api('PATCH', `/api/v1/publish/${encodeURIComponent(slug)}/access`, payload);
    if (ok(acc)) log(`  access: ${acc.json?.access?.mode || payload.mode}`);
    else log(`  ⚠ could not set access "${access}": ${explain(acc, 'Access')}`);
  }

  // 7. Report (publishStatus from finalize is authoritative for persistence)
  const url = f.primaryUrl || f.accountUrl || f.siteUrl || site.primaryUrl || site.siteUrl;
  const ps = f.publishStatus || {};
  const expiresAt = ps.expiresAt ?? site.expiresAt ?? null;
  const claimUrl = created ? site.claimUrl || null : state.claimUrl || null;

  let live = 0;
  let liveBlocked = false;
  for (let i = 0; i < 3 && live !== 200 && !liveBlocked; i++) {
    if (i) sleep(2000);
    try {
      const out = execFileSync('curl', ['-sS', '-w', '\n%{http_code}', '--max-time', '20', url],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 32 << 20 });
      const cut = out.lastIndexOf('\n');
      live = Number(out.slice(cut + 1));
      liveBlocked = proxyBlocked(live, out.slice(0, cut));
    } catch { live = 0; }
  }

  if (MODE === 'preview') {
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({
      api: API, slug, url, authed: AUTHED, workspace: WORKSPACE || null,
      claimToken: created ? site.claimToken || null : state.claimToken || null,
      claimUrl, expiresAt, updatedAt: new Date().toISOString(),
    }, null, 2)}\n`);
  }

  const lasts = !expiresAt && ps.persistence === 'permanent' ? 'permanent'
    : expiresAt ? `until ${new Date(expiresAt).toUTCString()}${AUTHED ? '' : ' (anonymous: 24 hours)'}`
      : AUTHED ? (MODE === 'production' || MODE === 'development' ? 'permanent' : '7 days after the last publish') : '24 hours (anonymous)';
  const liveNote = live === 200 ? 'OK (HTTP 200)'
    : liveBlocked ? 'not checked (*.here.now is not in Allowed domains; the link itself is fine)'
      : live ? `HTTP ${live} (expected if the site requires sign-in)` : 'not checked (could not reach *.here.now from here)';

  console.log([
    '',
    `✔ Published to here.now (${MODE}${created ? ', new site' : ''})`,
    `  Link:       ${url}`,
    `  Lasts:      ${lasts}`,
    `  Live check: ${liveNote}`,
    ...(claimUrl && !AUTHED ? [`  Keep it:    ${claimUrl}`, '              (claim link; open it and sign in to keep this site past 24 hours)'] : []),
    '',
    `HERENOW_URL=${url}`,
    `HERENOW_SLUG=${slug}`,
  ].join('\n'));

  if (env.GITHUB_OUTPUT) fs.appendFileSync(env.GITHUB_OUTPUT, `url=${url}\nslug=${slug}\ncreated=${created}\n`);
  if (env.GITHUB_STEP_SUMMARY) {
    const lines = [`### here.now ${MODE}`, '', `- Link: ${url}`, `- Slug: \`${slug}\``, `- Lasts: ${lasts}`];
    if ((MODE === 'production' || MODE === 'development') && created) {
      const v = MODE === 'production' ? 'HERENOW_PROD_SLUG' : 'HERENOW_DEV_SLUG';
      lines.push('', `**First ${MODE} deploy.** Set repository variable \`${v}\` to \`${slug}\` ` +
        '(Settings > Secrets and variables > Actions > Variables) so future merges update this site instead of failing.');
    }
    fs.appendFileSync(env.GITHUB_STEP_SUMMARY, `${lines.join('\n')}\n`);
  }
}

main().catch((e) => die(e.stack || e.message));
