const { chromium } = require('playwright-core');
const http = require('node:http');
const fs = require('node:fs');

(async () => {
  const server = http.createServer((req, res) => {
    if (req.url === '/api/double') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ value: 21 * 2 }));
      return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end('<button id="calculate">Calculer</button><output id="result"></output>' +
      '<script>document.querySelector("button").onclick=async()=>{' +
      'const r=await fetch("/api/double");document.querySelector("output").textContent=(await r.json()).value;}</script>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH,
      headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.click('#calculate');
    await page.waitForFunction(() => document.querySelector('#result').textContent === '42');
    await page.screenshot({ path: 'browser.png' });
    const proof = { value: 42, browser: await browser.version(), node: process.version, arch: process.arch };
    fs.writeFileSync('browser-proof.json', JSON.stringify(proof));
    console.log(JSON.stringify(proof));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
