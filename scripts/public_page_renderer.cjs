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
    return path === "/" || /(?:job(?:\/|$)|joblist|job\/list|jobdetail|job-detail|job\/detail|position|positions|campus|article|news)/i.test(path)
      || /(?:channel=campus|channel=social|page=|pageindex=|jobid=|positionid=|keyword=)/i.test(url.search);
  }
  if (sourceId === "boss") {
    if (!(host === "zhipin.com" || host.endsWith(".zhipin.com"))) return false;
    if (/\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)/i.test(path)) return false;
    if (/\/(?:login|register|resume|member|account|passport|captcha|app)(?:\/|$)/i.test(path)) return false;
    return /^\/(?:school|xiaoyuan)\/?$/i.test(path) || /^\/(?:school|xiaoyuan)\//i.test(path);
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

    const bossSnapshots = [];
    // Read a bounded set of tabs that are visibly rendered on BOSS's public campus hub.
    // Only exact labels are clicked; category navigations must stay in the /school or
    // /xiaoyuan public area. No job details, login, delivery or security pages are opened.
    if (sourceId === "boss" && /^\/(?:school|xiaoyuan)\/?$/i.test(new URL(requestedUrl).pathname)) {
      const seenBodies = new Set();
      const capture = async (label) => {
        const snap = await page.evaluate(() => {
          const body = document.body;
          if (!body) return { title: document.title || "", text: "", html: "", linkCount: 0 };
          const clone = body.cloneNode(true);
          clone.querySelectorAll("script,style,noscript,iframe,svg").forEach(el => el.remove());
          clone.querySelectorAll("[hidden],[aria-hidden='true']").forEach(el => el.remove());
          clone.querySelectorAll("a[href]").forEach(anchor => {
            const href = anchor.getAttribute("href");
            try { if (href) anchor.setAttribute("href", new URL(href, location.href).href); } catch {}
          });
          return { title: document.title || "", text: (body.innerText || "").trim(),
            html: clone.innerHTML, linkCount: clone.querySelectorAll("a[href]").length };
        });
        if (snap.text.length > 180 && !seenBodies.has(snap.text)) {
          seenBodies.add(snap.text);
          bossSnapshots.push({ label, ...snap });
        }
      };
      await capture("默认校招");
      const labels = ["AI", "算法", "互联网", "研发", "产品", "运营", "管培生", "职能", "销售", "国企", "银行", "教师", "外企"];
      for (const label of labels) {
        const bodyBefore = await page.locator("body").innerText().catch(() => "");
        if (hasSecurityInterstitial(page.url(), await page.title().catch(() => ""), bodyBefore)) break;
        if (page.url() !== requestedUrl) {
          await page.goto(requestedUrl, { waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
          await page.waitForTimeout(250);
        }
        const beforeText = await page.locator("body").innerText().catch(() => "");
        const pattern = new RegExp("^\\s*" + label + "\\s*$", "i");
        const options = page.locator("a,button,[role='tab'],[role='button']").filter({ hasText: pattern });
        const count = await options.count().catch(() => 0);
        let picked = null;
        for (let i = 0; i < Math.min(count, 8); i++) {
          const option = options.nth(i);
          if (!(await option.isVisible().catch(() => false))) continue;
          const href = await option.getAttribute("href").catch(() => "");
          if (href && !href.startsWith("#") && !/^javascript:/i.test(href)) {
            let target = "";
            try { target = new URL(href, page.url()).href; } catch {}
            if (!target || !allowPublicUrl("boss", target)) continue;
          }
          picked = option;
          break;
        }
        if (!picked) continue;
        await picked.click({ timeout: 1200 }).catch(() => {});
        await page.waitForFunction(previous => document.body && (document.body.innerText || "").trim() !== previous,
          beforeText, { timeout: 1800 }).catch(() => {});
        await page.waitForTimeout(300);
        if (!allowPublicUrl("boss", page.url())) break;
        const afterText = await page.locator("body").innerText().catch(() => "");
        if (hasSecurityInterstitial(page.url(), await page.title().catch(() => ""), afterText)) break;
        if (afterText.length > 180 && afterText !== beforeText) await capture(label);
        if (page.url() !== requestedUrl) {
          await page.goto(requestedUrl, { waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
          await page.waitForLoadState("networkidle", { timeout: 1200 }).catch(() => {});
          await page.waitForTimeout(250);
        }
      }
    }

    // Some 51job public campus microsites use visible JS tabs (including javascript: links).
    // Click only a visible recruitment-navigation label; never click login, delivery, account,
    // or security controls.
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
    let outputHtml = observed.html;
    let outputText = observed.bodyText;
    let outputLinkCount = observed.linkCount;
    if (sourceId === "boss" && bossSnapshots.length > 1) {
      outputHtml = "<!doctype html><html><head><title>BOSS public campus category snapshots</title></head><body>"
        + bossSnapshots.map(item => "<section data-boss-category=\"" + item.label + "\">" + item.html + "</section>").join("")
        + "</body></html>";
      outputText = bossSnapshots.map(item => "[" + item.label + "] " + item.text).join("\n\n");
      outputLinkCount = bossSnapshots.reduce((sum, item) => sum + item.linkCount, 0);
    }
    const security = hasSecurityInterstitial(finalUrl, observed.title, outputText);
    return {
      ok: true, html: outputHtml, final_url: finalUrl, title: observed.title,
      body_chars: outputText.length, body_preview: outputText.slice(0, 350), link_count: outputLinkCount,
      category_snapshots: bossSnapshots.length, category_tabs_captured: bossSnapshots.map(item => item.label),
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
