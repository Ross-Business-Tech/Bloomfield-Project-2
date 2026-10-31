#!/usr/bin/env node
/**
 * herenow-login.mjs: sign the student in to here.now from a Claude Code session using
 * here.now's agent email-code flow (https://here.now/docs#auth-signup), save the key for
 * this session, and check they belong to the team workspace.
 *
 *   node scripts/herenow-login.mjs start  <email>          # here.now emails a one-time code
 *   node scripts/herenow-login.mjs finish <email> <code>   # verify code, save key, check workspace
 *   node scripts/herenow-login.mjs status                  # signed in? member of the workspace?
 *
 * The key goes to ~/.herenow/credentials (mode 600), where scripts/herenow-publish.mjs and the
 * official here.now tooling read it. It is never printed, so it never lands in the session
 * transcript. It lives only as long as this session's VM; a new session signs in again, and
 * because a key name is sent, here.now returns the same key rather than minting a new one.
 *
 * Env: HERENOW_WORKSPACE (workspace to check), HERENOW_KEY_NAME (default claude-code-web),
 *      HERENOW_API_BASE (default https://here.now; used by admin/tests).
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const env = process.env;
const API = (env.HERENOW_API_BASE || 'https://here.now').replace(/\/+$/, '');
const WORKSPACE = (env.HERENOW_WORKSPACE || '').trim().toLowerCase()
  .replace(/^https?:\/\//, '').replace(/\.here\.now\/?$/, '');
const KEY_NAME = env.HERENOW_KEY_NAME || 'claude-code-web';
const CLIENT = 'claude-code/ross-herenow-login';
const CRED_DIR = path.join(os.homedir(), '.herenow');
const CRED = path.join(CRED_DIR, 'credentials');
const [cmd, email, code] = process.argv.slice(2);

function die(msg) { console.error(`✖ ${msg}`); process.exit(1); }

function call(method, p, payload, key) {
  const args = ['-sS', '-X', method, API + p, '--max-time', '30', '-H', `X-HereNow-Client: ${CLIENT}`,
    '-H', 'accept: application/json', '-w', '\n%{http_code}'];
  let tmp = '';
  if (key) { // key via 0600 header file, never on curl's argv
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'herenow-'));
    fs.writeFileSync(path.join(tmp, 'h'), `Authorization: Bearer ${key}\n`, { mode: 0o600 });
    args.push('-H', `@${path.join(tmp, 'h')}`);
  }
  let input;
  if (payload !== undefined) { args.push('-H', 'content-type: application/json', '--data-binary', '@-'); input = JSON.stringify(payload); }
  try {
    const out = execFileSync('curl', args, { input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] });
    const cut = out.lastIndexOf('\n');
    const text = out.slice(0, cut);
    let json = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: Number(out.slice(cut + 1)), json, text };
  } catch (e) {
    return { status: 0, json: null, text: String(e.stderr || e.message).trim() };
  } finally {
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  }
}

const ok = (r) => r.status >= 200 && r.status < 300;
function fail(res, what) {
  if (res.status === 0) die(`${what}: could not reach ${API} (${res.text.split('\n').pop()}).`);
  if (res.status === 403 && !res.json) {
    die(`${what}: the network blocked ${API}. here.now must be in this environment's Allowed domains.`);
  }
  const j = res.json || {};
  const detail = [j.code && `(${j.code})`, j.error, j.message && j.message !== j.error ? j.message : '']
    .filter(Boolean).join(' ') || res.text.slice(0, 200);
  die(`${what}: HTTP ${res.status} ${detail}`);
}

const validEmail = (e) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e || '');
function savedKey() {
  if ((env.HERENOW_API_KEY || '').trim()) return env.HERENOW_API_KEY.trim();
  try { return fs.readFileSync(CRED, 'utf8').trim(); } catch { return ''; }
}

// Response shape of GET /api/v1/accounts isn't pinned in the docs beyond "subdomain, role,
// selector" per account, so search for the subdomain rather than assuming a field layout.
function hasSubdomain(node, target, skipKey) {
  if (Array.isArray(node)) return node.some((n) => hasSubdomain(n, target, skipKey));
  if (node && typeof node === 'object') {
    if (String(node.subdomain || '').toLowerCase() === target) return true;
    return Object.entries(node).some(([k, v]) => k !== skipKey && hasSubdomain(v, target, skipKey));
  }
  return false;
}

function checkWorkspace(key) {
  if (!WORKSPACE) return;
  const res = call('GET', '/api/v1/accounts?includeJoinable=1', undefined, key);
  if (!ok(res) || !res.json) {
    console.log(`  Workspace: could not check membership of "${WORKSPACE}" (HTTP ${res.status}).`);
    return;
  }
  if (hasSubdomain(res.json, WORKSPACE, 'joinable')) {
    console.log(`  Workspace: member of ${WORKSPACE} ✔ (previews will publish there)`);
  } else if (hasSubdomain(res.json.joinable, WORKSPACE)) {
    console.log(`  Workspace: NOT a member of ${WORKSPACE} yet, but this email is allowed to join it.\n` +
      '  Ask the student to sign in at https://here.now/dashboard with the same email and click Join,\n' +
      '  then run: node scripts/herenow-login.mjs status');
  } else {
    console.log(`  Workspace: NOT a member of ${WORKSPACE}. Previews cannot publish there yet.\n` +
      '  Ask the student to accept the workspace invitation email from here.now, or to ask their\n' +
      '  instructor to invite this exact email address. Then run: node scripts/herenow-login.mjs status');
  }
}

if (cmd === 'start') {
  if (!validEmail(email)) die('usage: node scripts/herenow-login.mjs start <email>');
  const res = call('POST', '/api/auth/agent/request-code', { email });
  if (!ok(res)) fail(res, 'Requesting a sign-in code');
  console.log(`✔ here.now sent a sign-in code to ${email}.\n  Ask the student to paste the code from that email, then run:\n` +
    `  node scripts/herenow-login.mjs finish ${email} <code>`);
} else if (cmd === 'finish') {
  if (!validEmail(email) || !code) die('usage: node scripts/herenow-login.mjs finish <email> <code>');
  const res = call('POST', '/api/auth/agent/verify-code', { email, code: code.trim(), keyName: KEY_NAME });
  if (!ok(res)) fail(res, 'Verifying the code');
  const key = res.json?.apiKey;
  if (!key) die('here.now accepted the code but returned no API key.');
  fs.mkdirSync(CRED_DIR, { recursive: true, mode: 0o700 });
  fs.writeFileSync(CRED, `${key}\n`, { mode: 0o600 });
  fs.chmodSync(CRED, 0o600);
  console.log(`✔ Signed in to here.now as ${email} (key …${key.slice(-4)}, name "${KEY_NAME}"` +
    `${res.json.keyReused ? ', reused' : ''}). Saved for this session.`);
  if (res.json.keyMintError) {
    console.log(`  ⚠ here.now returned the account's original key instead of a named one (${res.json.keyMintError}).`);
  }
  checkWorkspace(key);
} else if (cmd === 'status') {
  const key = savedKey();
  if (!key) {
    console.log(`Not signed in to here.now in this session.${WORKSPACE ? ` Needed to publish into the ${WORKSPACE} workspace.` : ''}`);
    process.exit(0);
  }
  const res = call('GET', '/api/v1/accounts', undefined, key);
  if (res.status === 401) die('the saved here.now key was rejected. Sign in again with: node scripts/herenow-login.mjs start <email>');
  if (!ok(res)) fail(res, 'Checking the saved key');
  console.log(`✔ Signed in to here.now (key …${key.slice(-4)}).`);
  checkWorkspace(key);
} else {
  die('usage: node scripts/herenow-login.mjs start <email> | finish <email> <code> | status');
}
