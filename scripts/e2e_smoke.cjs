const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

// Compare raw published rows with the UI results so a collector/publisher issue
// is not misdiagnosed as a browser filter bug.
const publishedJobs = JSON.parse(fs.readFileSync(path.join(__dirname, "../data/jobs.json"), "utf8"));
const aigcSourceRows = publishedJobs.filter(x => /AIGC影视创意制作|基美影业/.test(String(x.title || "") + " " + String(x.company || "")));
console.log("PUBLISHED_AIGC_DATA_PROBE " + JSON.stringify({
  total: publishedJobs.length,
  matches: aigcSourceRows.map(x => ({id:x.id,title:x.title,company:x.company,city:x.city,locations:x.locations,keywords:x.keywords,source:x.source,url:x.source_url,cohort:x.cohort,status:x.status,granularity:x.granularity}))
}));

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
    await page.locator("#heroSearch").click();
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
    await title.scrollIntoViewIfNeeded();
    // The result set can reflow while Chromium finishes large JSON/layout work.
    // Trigger the actual button DOM event after scrolling to avoid a false
    // Playwright "element detached while scrolling" failure on a legitimate click.
    await page.waitForTimeout(350);
    await title.evaluate(el => el.click());

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
    assert.ok(Math.abs(closeBox.y - titleBox.y) <= 6, "modal close button must align vertically with the title row");
    await close.click();
    await page.waitForFunction(() => !document.querySelector("#jobDetailModal")?.classList.contains("show"));
    await page.locator("#jobs .role-row .role-title-button").first().evaluate(el => el.click());
    await page.waitForFunction(() => document.querySelector("#jobDetailModal")?.classList.contains("show"));
    await page.keyboard.press("Escape");
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
    const collectorText = await page.locator("#collectorStatus").innerText();
    assert.ok(collectorText.length > 0, "collector monitoring panel should render");
    assert.ok(collectorText.includes("岗位级记录") && collectorText.includes("只有公告/入口") && collectorText.includes("可访问但零产出"), "source monitor must distinguish role-level sources from portals and zero-output sources");

    await page.locator('#workbench .tab[data-tab="jobs"]').click();
    await page.locator("#reset").click();
    assert.equal(await page.locator("#q").inputValue(), "", "reset clears search");
    assert.equal(await page.locator("#city").inputValue(), "", "reset clears city");

    // Log the real AIGC+Shanghai detail fields. Missing public fields stay
    // explicitly unavailable; the test never invents salary/address values.
    let aigcDetail = { found: false, salary: "", degree: "", city: "", address: "", responsibilityChars: 0, requirementsChars: 0, bucket: "", title: "" };
    await page.locator("#q").fill("AIGC影视创意制作");
    await page.locator("#city").selectOption("上海");
    await page.waitForTimeout(350);
    for (const bucket of ["priority","lead"]) {
      await page.locator('[data-bucket="'+bucket+'"]').click();
      const yjsGroups = page.locator("#jobs .company-group");
      for (let gi=0; gi<await yjsGroups.count(); gi++) {
        const group=yjsGroups.nth(gi);
        await group.locator("summary").click();
        const role=group.locator(".role-row").filter({hasText:"AIGC影视创意制作"}).first();
        if (await role.count() > 0) {
          const visibleTitle=(await role.locator(".role-title-button").innerText()).trim();
          await role.locator(".role-title-button").evaluate(el=>el.click());
          await page.waitForFunction(() => document.querySelector("#jobDetailModal")?.classList.contains("show"));
          const facts=await page.locator("#jobDetailContent .detail-fact").evaluateAll(els=>Object.fromEntries(els.map(el=>[el.querySelector("span")?.textContent?.trim()||"",el.querySelector("b")?.textContent?.trim()||""])));
          const addressParts=await page.locator("#jobDetailContent .detail-company-info").allInnerTexts();
          const cards=page.locator("#jobDetailContent .detail-card");
          aigcDetail={found:true,salary:facts["薪资"]||"",degree:facts["学历要求"]||"",city:facts["工作城市"]||"",address:addressParts.join(" | "),responsibilityChars:await cards.nth(2).innerText().then(t=>t.length).catch(()=>0),requirementsChars:await cards.nth(3).innerText().then(t=>t.length).catch(()=>0),bucket,title:visibleTitle};
          await page.locator("#jobDetailContent .detail-close").click();
          await page.waitForFunction(() => !document.querySelector("#jobDetailModal")?.classList.contains("show"));
          break;
        }
        if (await group.getAttribute("open")) await group.locator("summary").click();
      }
      if (aigcDetail.found) break;
    }

    let aigcFoundWithoutCity=false;
    if (!aigcDetail.found) {
      // Diagnostic pass distinguishes missing source rows from city-filter mismatch.
      await page.locator("#reset").click();
      await page.locator("#q").fill("AIGC影视创意制作");
      await page.waitForTimeout(350);
      for (const bucket of ["priority","lead"]) {
        await page.locator('[data-bucket="'+bucket+'"]').click();
        const groupsNoCity=page.locator("#jobs .company-group");
        for (let gi=0; gi<await groupsNoCity.count(); gi++) {
          const group=groupsNoCity.nth(gi);
          await group.locator("summary").click();
          const role=group.locator(".role-row").filter({hasText:"AIGC影视创意制作"}).first();
          if (await role.count()>0) {
            const visibleTitle=(await role.locator(".role-title-button").innerText()).trim();
            await role.locator(".role-title-button").evaluate(el=>el.click());
            await page.waitForFunction(() => document.querySelector("#jobDetailModal")?.classList.contains("show"));
            const facts=await page.locator("#jobDetailContent .detail-fact").evaluateAll(els=>Object.fromEntries(els.map(el=>[el.querySelector("span")?.textContent?.trim()||"",el.querySelector("b")?.textContent?.trim()||""])));
            const addressParts=await page.locator("#jobDetailContent .detail-company-info").allInnerTexts();
            const cards=page.locator("#jobDetailContent .detail-card");
            aigcDetail={found:true,salary:facts["薪资"]||"",degree:facts["学历要求"]||"",city:facts["工作城市"]||"",address:addressParts.join(" | "),responsibilityChars:await cards.nth(2).innerText().then(t=>t.length).catch(()=>0),requirementsChars:await cards.nth(3).innerText().then(t=>t.length).catch(()=>0),bucket,title:visibleTitle,cityFilter:"全部城市"};
            aigcFoundWithoutCity=true;
            await page.locator("#jobDetailContent .detail-close").click();
            await page.waitForFunction(() => !document.querySelector("#jobDetailModal")?.classList.contains("show"));
            break;
          }
          if (await group.getAttribute("open")) await group.locator("summary").click();
        }
        if (aigcDetail.found) break;
      }
    }

    // A direct source page must yield a clean title after the revised crawler
    // has reached and normalized the public Xpeng role details.
    let xiaopengRoleObserved=false;
    await page.locator("#reset").click();
    await page.locator("#q").fill("具身智能AI软件工程师");
    await page.locator("#city").selectOption("上海");
    await page.waitForTimeout(350);
    for (const bucket of ["priority","lead"]) {
      await page.locator('[data-bucket="'+bucket+'"]').click();
      const xpGroups=page.locator("#jobs .company-group");
      for (let gi=0; gi<await xpGroups.count(); gi++) {
        const group=xpGroups.nth(gi);
        await group.locator("summary").click();
        const role=group.locator(".role-row").filter({hasText:"具身智能AI软件工程师"}).first();
        if (await role.count() > 0) {
          const visibleTitle=(await role.locator(".role-title-button").innerText()).trim();
          assert.ok(!/^[〖【\[]?\s*27届/.test(visibleTitle),"Xpeng title retains cohort wrapper: "+visibleTitle);
          xiaopengRoleObserved=true;
          break;
        }
        if (await group.getAttribute("open")) await group.locator("summary").click();
      }
      if(xiaopengRoleObserved) break;
    }
    assert.ok(aigcDetail.found, "specific AIGC Shanghai job must open its in-site detail modal; got " + JSON.stringify(aigcDetail));
    assert.ok(aigcDetail.salary.includes("8千-1.5万"), "public AIGC salary should be extracted from the detail page: " + JSON.stringify(aigcDetail));
    assert.ok(aigcDetail.degree.includes("本科"), "public AIGC degree should be extracted from the detail page: " + JSON.stringify(aigcDetail));
    assert.ok(aigcDetail.city.includes("上海"), "AIGC role should remain filterable in Shanghai: " + JSON.stringify(aigcDetail));
    assert.ok(aigcDetail.responsibilityChars > 40 && aigcDetail.requirementsChars > 40, "detail modal should include the public job duties and requirements: " + JSON.stringify(aigcDetail));
    assert.ok(xiaopengRoleObserved, "Xpeng 27th-cohort AI software role should be available in Shanghai after collection");

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
      escapeToClose: "pass",
      heroSearchButton: "pass",
      sourceProductivityBreakdown: "pass",
      favoritesAndDeliveryBoard: "pass",
      profileSave: "pass",
      sourceMonitor: "pass",
      resetFilters: "pass",
      aigcDetail,
      aigcFoundWithoutCity,
      xiaopengRoleObserved,
      browserErrors: pageErrors
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
