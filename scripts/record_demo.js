// Records the README demo: a faulty batch import, live MQTT and IEC 104 feeds with
// their faults, then the corrected import closing the mapping issue.
// Usage: node scripts/record_demo.js <outdir> [base-url]
// Needs puppeteer-core, a local Chrome, and the stack from the README running
// (API on base-url; this script starts the simulator and the imports itself).
const puppeteer = require("puppeteer-core");
const { spawn, execFileSync } = require("child_process");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [outdir, BASE = "http://localhost:8020"] = process.argv.slice(2);
const ROOT = path.resolve(__dirname, "..");
const PY = path.join(ROOT, ".venv/bin/python");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const W = 1440, H = 900;

const CURSOR = `
(() => {
  const add = () => {
    if (document.getElementById('demo-cursor')) return;
    const c = document.createElement('div');
    c.id = 'demo-cursor';
    c.style.cssText = 'position:fixed;left:-50px;top:-50px;width:18px;height:18px;border-radius:50%;background:rgba(255,255,255,0.9);border:2px solid #0b0f14;box-shadow:0 0 0 3px rgba(126,226,168,0.55);z-index:99999;pointer-events:none;transform:translate(-50%,-50%)';
    document.body.appendChild(c);
    document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  };
  if (document.body) add(); else document.addEventListener('DOMContentLoaded', add);
})();`;

async function pick(page, device) {
  // The dashboard re-renders every 1.5 s, so look the element up again right before each step.
  const sel = `[data-d="${device}"]`;
  await page.waitForSelector(sel, { visible: true });
  await page.$eval(sel, (n) => n.scrollIntoView({ block: "center", behavior: "smooth" }));
  await sleep(400);
  let box = null;
  for (let k = 0; k < 20 && !box; k++) {
    const el = await page.$(sel);
    box = el && (await el.boundingBox());
    if (!box) await sleep(100);
  }
  await page.mouse.move(box.x + 60, box.y + box.height / 2, { steps: 18 });
  await sleep(250);
  await page.$eval(sel, (n) => n.click());
}

async function waitFor(page, device, minMessages) {
  // Choreography follows the data, not the clock: wait until the device has sent enough readings.
  for (;;) {
    const n = await page.evaluate(async (d) => {
      const o = await (await fetch("/api/overview")).json();
      return (o.devices.find((x) => x.device === d) || {}).messages || 0;
    }, device);
    if (n >= minMessages) return;
    await sleep(250);
  }
}

function py(...args) {
  return execFileSync(PY, args, { cwd: ROOT, encoding: "utf8" });
}

(async () => {
  console.log(py("-m", "gridfeed.backfill", "import", "data/exports/week_v1.csv"));
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: [`--window-size=${W},${H}`, "--hide-scrollbars"], defaultViewport: { width: W, height: H } });
  const page = await browser.newPage();
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  await page.evaluateOnNewDocument(CURSOR);
  await page.goto(`${BASE}/?device=ONS%20Rural%201`, { waitUntil: "networkidle0" });
  const rec = await page.screencast({ path: path.join(outdir, "demo.webm") });
  await sleep(4500); // the swapped export: fingerprint says Rural 1 is mapped to the wrong transformer

  const sim = spawn(PY, ["-m", "gridfeed.sim", "--interval", "0.18"], { cwd: ROOT, stdio: "ignore" });
  await sleep(3500);
  await waitFor(page, "SA-1001", 40);
  await pick(page, "SA-1001"); // live: the midday PV export pushes the transformer over 100%
  await waitFor(page, "SA-1001", 62);
  await sleep(1500);
  await waitFor(page, "SA-1002", 112);
  await pick(page, "SA-1002"); // the voltage sensor freezes (readings 96 to 127)
  await waitFor(page, "SA-1002", 134);
  await pick(page, "GB-0402"); // the gateway goes quiet (readings 150 to 161)
  await waitFor(page, "SA-1001", 156);
  await sleep(2500);
  await waitFor(page, "GB-0301", 176);
  await pick(page, "GB-0301"); // calibration drift from reading 144
  await sleep(4000);
  await waitFor(page, "RTU-CA6", 206);
  await pick(page, "RTU-CA6"); // IEC 104: a 0 V reading earlier, stale flags at readings 200 to 203
  await sleep(4000);
  await new Promise((r) => sim.on("exit", r));

  await pick(page, "ONS Rural 1");
  await sleep(2500);
  py("-m", "gridfeed.backfill", "import", "data/exports/week_v2.csv"); // the customer fixes the export
  await sleep(6000);
  await page.screenshot({ path: path.join(outdir, "end.png") });
  await Promise.race([rec.stop(), sleep(45000)]); // let the encoder flush the last frames
  await Promise.race([browser.close(), sleep(5000)]);
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
