const readline = require("node:readline");
const { chromium } = require("playwright");

let browserPromise;
const USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36";

function allowPublicUrl(sourceId, value) {
  let url;
  try { url = new URL(value); } catch { return false; }
  const host = url.hostname.toLowerCase();
  const path = url.pathname.toLowerCase();
  if (url.protocol !== "https:") return false;
  if (sourceId === "iguopin") {
    if (!(host === "iguopin.com" || host.endsWith(".iguopin.com"))) return false;
    if (/\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)/i.test(path)) return false;
    if (/\/(?:login|register|resume|member|account|passport|captcha)(?:\/|$)/i.test(path)) return false;
    return path === "/" || /(?:joblist|job\/list|jobdetail|job-detail|job\/detail|position|positions|campus|article|news)/i.test(path)
      || /(?:channel=campus|channel=social|page=|pageindex=|jobid=|positionid=|keyword=)/i.test(url.search);
  }
  if (sourceId === "51job") {
    if (!(host === "51job.com" || host.endsWith(".51job.com"))) return false;
    if (/\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)/i.test(path)) return false;
    if (/\/(?:login|register|resume|member|account|passport|captcha)(?:\/|$)/i.test(path)) return false;
    return path === "/" || path.endsWith(".html") || path.endsWith(".php")
      || /(?:jobs?|jobdetail|job-detail|position|positions|campus|company|page|pageno|jobid|positionid)/i.test(path + url.search);
  }
  return false;
}

function hasSecurityInterstitial(finalUrl, title, bodyText) {
  const probe = (String(finalUrl || "") + " " + String(title || "") + " " + String(bodyText || "")).toLowerCase();
  return /captcha|security\.html|安全验证|请完成验证|身份验证|访问过于频繁|访问受限|人机验证|验证码/.test(probe)
    || /\/passport\/.*security/.test(String(finalUrl || "").toLowerCase());
}

async function getBrowser() {
  if (!browserPromise) {
    browserPromise = chromium.launch({ headless: true, args: ["--no-sandbox"] });
  }
  return browserPromise;
}

async function render(request) {
  const sourceId = String(request.source_id || "");
  const requestedUrl = String(request.url || "");
  if (!allowPublicUrl(sourceId, requestedUrl)) {
    return { ok: false, error: "URL is outside the anonymous public-page allow-list" };
  }
  let context;
  try {
    const browser = await getBrowser();
    context = await browser.newContext({ userAgent: USER_AGENT, viewport: { width: 1365, height: 900 } });
    const page = await context.newPage();
    page.setDefaultNavigationTimeout(18000);
    page.setDefaultTimeout(6000);
    const response = await page.goto(requestedUrl, { waitUntil: "domcontentloaded", timeout: 18000 }).catch(() => null);
    await page.waitForLoadState("networkidle", { timeout: 2500 }).catch(() => {});
    await page.waitForFunction(() => {
      if (!document.body) return false;
      const text = (document.body.innerText || "").trim();
      return text.length > 180 || document.querySelectorAll("a[href]").length > 8;
    }, { timeout: 4500 }).catch(() => {});
    await page.waitForTimeout(700);

    // Some 51job public campus microsites use visible JS tabs (including javascript: links)
    // instead of real URLs. Click only a visible recruitment-navigation label; never click
    // login, delivery, account, or security controls.
    if (sourceId === "51job") {
      const tabPattern = /^\s*(校园招聘|招聘岗位|招聘职位|职位列表|岗位列表|职位信息|岗位信息|职位介绍|职位)\s*$/;
      const tab = page.locator("a,button,[role='tab']").filter({ hasText: tabPattern }).first();
      if (await tab.count().catch(() => 0)) {
        const visible = await tab.isVisible().catch(() => false);
        if (visible) {
          await tab.click({ timeout: 1200 }).catch(() => {});
          await page.waitForLoadState("networkidle", { timeout: 1800 }).catch(() => {});
          await page.waitForFunction(() => document.body && ((document.body.innerText || "").trim().length > 180 || document.querySelectorAll("a[href]").length > 8), { timeout: 3000 }).catch(() => {});
          await page.waitForTimeout(500);
        }
      }
    }

    const currentUrl = page.url();
    if (!allowPublicUrl(sourceId, currentUrl)) {
      return {
        ok: true, html: "<!doctype html><html><head><title>Public page redirected outside allow-list</title></head><body></body></html>",
        final_url: currentUrl, title: await page.title().catch(() => ""),
        body_chars: 0, link_count: 0, body_preview: "", http_status: 0,
        security_interstitial: true, access_limited: true
      };
    }
    const observed = await page.evaluate(() => {
      const title = document.title || "";
      const bodyText = document.body ? (document.body.innerText || "").slice(0, 18000) : "";
      const linkCount = document.querySelectorAll("a[href]").length;
      const clone = document.documentElement.cloneNode(true);
      clone.querySelectorAll("script,style,noscript,iframe,svg").forEach(el => el.remove());
      clone.querySelectorAll("[hidden],[aria-hidden='true']").forEach(el => el.remove());
      return { title, bodyText, bodyPreview: bodyText.slice(0, 350), linkCount, html: "<!doctype html>" + clone.outerHTML };
    });
    const finalUrl = page.url();
    const security = hasSecurityInterstitial(finalUrl, observed.title, observed.bodyText);
    return {
      ok: true, html: observed.html, final_url: finalUrl, title: observed.title,
      body_chars: observed.bodyText.length, body_preview: observed.bodyPreview, link_count: observed.linkCount,
      http_status: response ? response.status() : 0, security_interstitial: security, access_limited: false
    };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error).slice(0, 220) };
  } finally {
    if (context) await context.close().catch(() => {});
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", line => {
  let request;
  try { request = JSON.parse(line); }
  catch { process.stdout.write(JSON.stringify({ ok: false, error: "invalid request JSON" }) + "\n"); return; }
  render(request).then(result => {
    process.stdout.write(JSON.stringify(result) + "\n");
  }).catch(error => {
    process.stdout.write(JSON.stringify({ ok: false, error: String(error && error.message || error).slice(0, 220) }) + "\n");
  });
});
rl.on("close", async () => {
  if (browserPromise) {
    try { const browser = await browserPromise; await browser.close(); } catch {}
  }
});
