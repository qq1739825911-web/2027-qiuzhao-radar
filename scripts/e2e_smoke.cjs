const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));

  try {
    await page.goto("http://127.0.0.1:4173/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(() => {
      const count = document.querySelector("#count");
      return count && count.textContent.trim() !== "—" &&
        document.querySelector("#city") && document.querySelector("#city").options.length > 2;
    }, { timeout: 90000 });

    // Search AIGC in Shanghai; if priority matches are absent, the UI should
    // clearly switch to the separate "pending review" bucket, never mix rows.
    await page.locator("#q").fill("AIGC");
    await page.locator("#city").selectOption("上海");
    await page.waitForFunction(() => {
      const title = document.querySelector("#jobs .listing-heading h3")?.textContent || "";
      return document.querySelectorAll("#jobs .company-group").length > 0 ||
        title.includes("0 条");
    }, { timeout: 15000 });

    const groups = page.locator("#jobs .company-group");
    const groupCount = await groups.count();
    assert.ok(groupCount > 0, "AIGC + Shanghai search should return job/company matches in the database");
    const heading = await page.locator("#jobs .listing-heading h3").innerText();
    assert.ok(/优先核验岗位|待核验线索/.test(heading), "results must identify the current trust bucket");

    await groups.first().locator("summary").click();
    await page.locator("#jobs .role-row").first().waitFor({ state: "visible", timeout: 10000 });
    const role = page.locator("#jobs .role-row").first();
    const title = role.locator(".role-title-button").first();
    assert.ok((await title.innerText()).trim().length > 0, "role title must be readable");
    await title.click();

    const modal = page.locator("#jobDetailModal");
    await page.waitForFunction(() => document.querySelector("#jobDetailModal")?.classList.contains("show"));
    const detailText = await page.locator("#jobDetailContent").innerText();
    assert.ok(detailText.includes("岗位基本信息"), "in-site detail panel should open");
    assert.ok(detailText.includes("工作城市") && detailText.includes("学历要求"), "detail shows city and degree fields");
    assert.ok(!/(careerJobId|salaryMax|salaryMin|jobCity|latestProcessTime)/i.test(detailText), "serialized JSON must not leak into detail text");

    const close = page.locator("#jobDetailContent .detail-close");
    const closeBox = await close.boundingBox();
    const titleBox = await page.locator("#jobDetailContent .detail-title-wrap").boundingBox();
    assert.ok(closeBox && titleBox && closeBox.x > titleBox.x, "modal close button must align at the title's right side");
    await close.click();
    await page.waitForFunction(() => !document.querySelector("#jobDetailModal")?.classList.contains("show"));

    // Favorites, delivery status, profile, source monitor, and reset interactions.
    // Modal history navigation rerenders company groups collapsed by design.
    if (await page.locator("#jobs .role-row .star").count() === 0) {
      await page.locator("#jobs .company-group").first().locator("summary").click();
      await page.locator("#jobs .role-row .star").first().waitFor({ state: "visible", timeout: 10000 });
    }
    await page.locator("#jobs .role-row .star").first().click();
    await page.locator('#workbench .tab[data-tab="board"]').click();
    const status = page.locator("#board [data-app]").first();
    await status.waitFor({ state: "visible", timeout: 10000 });
    await status.selectOption({ label: "已投递" });
    assert.ok(await page.locator("#board").innerText().then(t => t.includes("已投递")), "delivery board should render");

    await page.locator('#workbench .tab[data-tab="profile"]').click();
    await page.locator("#major").fill("视觉传达设计");
    await page.locator("#saveProfile").click();
    const storedProfile = await page.evaluate(() => localStorage.getItem("radar_profile"));
    assert.ok(storedProfile && storedProfile.includes("视觉传达设计"), "profile should save locally");

    await page.locator('#workbench .tab[data-tab="sources"]').click();
    await page.locator("#collectorStatus").waitFor({ state: "visible" });
    await page.waitForTimeout(500);
    assert.ok((await page.locator("#collectorStatus").innerText()).length > 0, "collector monitoring panel should render");

    await page.locator('#workbench .tab[data-tab="jobs"]').click();
    await page.locator("#reset").click();
    assert.equal(await page.locator("#q").inputValue(), "", "reset clears search");
    assert.equal(await page.locator("#city").inputValue(), "", "reset clears city");
    assert.equal(pageErrors.length, 0, "no uncaught browser errors: " + pageErrors.join("; "));

    console.log(JSON.stringify({
      result: "PASS",
      companyGroupsForAIGCShanghai: groupCount,
      resultBucket: heading,
      detailModal: "pass",
      metadataLeak: "none",
      closeAlignment: "pass",
      favoritesAndDeliveryBoard: "pass",
      profileSave: "pass",
      sourceMonitor: "pass",
      resetFilters: "pass",
      browserErrors: pageErrors
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
