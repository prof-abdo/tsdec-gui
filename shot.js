const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPO = 'D:\\Projects\\Tsdec-gui';
const OUT = path.join(REPO, 'shots');
const ENC = path.join(REPO, 'testdata', 'enc.ts');
const CWL = path.join(REPO, 'testdata', 'sample.cwl');
const OUTTS = path.join(REPO, 'testdata', 'shot.out.ts');
const TSDEC = 'D:\\Projects\\Tsdec\\tsdec.exe';
const PORT = 8791;
const URL = `http://127.0.0.1:${PORT}/`;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  // the server runs for the length of this script and is torn down with it,
  // rather than being left behind by a background process
  const server = spawn('python', ['tsdec_gui.py', '--no-browser', '--port', String(PORT)],
    { cwd: REPO, env: { ...process.env, TSDEC_BIN: TSDEC }, stdio: 'ignore' });

  const up = async () => {
    for (let i = 0; i < 40; i++) {
      try {
        const r = await fetch(`${URL}api/state`);
        if (r.ok) return true;
      } catch (e) { /* not listening yet */ }
      await new Promise(r => setTimeout(r, 250));
    }
    return false;
  };

  if (!await up()) { console.error('server never came up'); server.kill(); process.exit(1); }

  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1180, height: 900 } });

    const errors = [];
    page.on('pageerror', e => errors.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

    await page.goto(URL, { waitUntil: 'networkidle' });
    await page.screenshot({ path: path.join(OUT, '01-idle.png'), fullPage: true });

    // fill the form the way a user would
    await page.fill('#input', ENC);
    await page.fill('#cwl', CWL);
    await page.fill('#output', OUTTS);
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(OUT, '02-filled.png'), fullPage: true });

    // pid survey
    await page.click('#analyze');
    await page.waitForFunction(
      () => {
        const card = document.querySelector('#pids-card');
        const rows = document.querySelector('#pid-rows');
        return card && !card.hidden && rows && rows.children.length > 0;
      }, { timeout: 30000 });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, '03-pids.png'), fullPage: true });

    // run it
    await page.click('#start');
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(OUT, '04-running.png'), fullPage: true });

    await page.waitForFunction(
      () => { const v = document.querySelector('#verdict'); return v && !v.hidden; },
      { timeout: 90000 });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, '05-done.png'), fullPage: true });

    // light theme
    await page.click('#theme');
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, '06-light.png'), fullPage: true });

    // narrow viewport
    await page.setViewportSize({ width: 520, height: 900 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT, '07-narrow.png'), fullPage: true });

    console.log(errors.length ? 'JS ERRORS:\n' + errors.join('\n') : 'no js errors');
  } finally {
    await browser.close();
    server.kill();
  }
})();
