"""Offline regression tests for public job parsing and quality buckets."""
import json
from datetime import date
from pathlib import Path
from collect_public_sources import (
    year, clean_job_title, is_direct_detail_url, is_concrete_role_title,
    extract_yingjiesheng_fields, extract_detail_fields_generic, Parser, sanitize_extracted_text,
    same_host, should_discover_public_url, is_specific_role_title, extract_boss_fields, extract_nowcoder_fields, canonical,
    select_rotation_batch, extract_qiuzhaowang_role_names, diagnostic_link_priority,
    extract_boss_card_fields, extract_boss_card_company, should_render_public_page,
    normalize_source_url, clean_yingjiesheng_company_name, split_role_title_candidates,
    is_boss_campus_card_relevant, should_queue_detail, extract_visible_public_role_leads,
    is_public_role_lead_title, public_lead_dedupe_key,
    should_purge_qzw_noise_record,
    source_role_metrics,
)
from quality_rules import assess, compact_json, should_publish_record
from verify_job_links import is_direct_detail as verifier_is_direct_detail, is_security_interstitial_url

COLLECTOR_SOURCE=(Path(__file__).resolve().parent/"collect_public_sources.py").read_text(encoding="utf-8")
RENDERER_SOURCE=(Path(__file__).resolve().parent/"public_page_renderer.cjs").read_text(encoding="utf-8")

def check(name, condition):
    if not condition:
        raise AssertionError(f"FAILED: {name}")
    print(f"PASS: {name}")

# Cohort must not be silently guessed as 2027.
check("publication date is not a cohort", year("发布时间：2026-09-28") == "")
check("two-digit cohort is normalized", year("27届校招岗位") == "2027届")
check("explicit 2027 cohort is detected", year("岗位面向2027届毕业生") == "2027届")
check("2027 campus announcement is treated as cohort evidence", year("兴业银行2027校园招聘公告") == "2027届")

# Platform/HR metadata must not pollute the stored role title.
clean_title = clean_job_title(
    "27届校招-算法工程师(西安)(J12268)_诺瓦星云校招_牛客网", "nowcoder"
)
check("Nowcoder title is cleaned", clean_title == "算法工程师(西安)")
noise_title = "诺瓦星云 3分钟前在线 西安诺瓦星云科技股份有限公司·HR 反馈率：30% | 反馈时长：4天"
check("HR status text is not a role title", not is_concrete_role_title(noise_title))
check("Bracketed 27-cohort title and application code are cleaned",
      clean_job_title("【27届校招】测试工程师(A54426)", "nowcoder") == "测试工程师")
check("Xpeng corner-bracket cohort marker is cleaned",
      clean_job_title("〖27届校招〗具身智能AI软件工程师", "xiaopeng") == "具身智能AI软件工程师")

check("Generic recruitment-site title is not a concrete role",
      not is_concrete_role_title("小鹏汽车招聘、福利待遇、工作环境丨小鹏汽车"))
parsed = Parser()
parsed.feed('<p>岗位职责：完成测试并整理报告。</p><script>{"jobCity":"北京","careerJobId":11025,"salaryMax":9999999}</script>')
check("embedded script JSON is excluded from visible page text",
      "岗位职责" in parsed.page_text() and "careerJobId" not in parsed.page_text())
dirty = r'岗位职责：完成测试并整理报告。\n3. 相关职责说明。","jobCity":"北京","careerJobId":11025,"salaryMax":9999999'
cleaned = sanitize_extracted_text(dirty)
check("historical description sanitizer removes embedded JSON",
      "岗位职责" in cleaned and "careerJobId" not in cleaned and "salaryMax" not in cleaned)
check("historical description sanitizer removes trailing serialization quotes",
      not cleaned.endswith(("\\", '"', "}")))
check("JSON-only embedded fragment is not shown as job description",
      sanitize_extracted_text(r'{"jobCity":"北京","careerJobId":11025}') == "")



# Direct URLs only; aggregator/search/list pages are not job details.
check("YJS numeric job detail recognized", is_direct_detail_url("https://m.yingjiesheng.com/jobdetail/170935981"))
check("Nowcoder detail recognized", is_direct_detail_url("https://www.nowcoder.com/jobs/detail/462503"))
check("Baidu uuid direct URL recognized", is_direct_detail_url("https://talent.baidu.com/jobs/detail/GRADUATE/a20dfb76-4c9d-4814-bd74-7561616e2773"))
check("Tencent detail post URL recognized", is_direct_detail_url("https://careers.tencent.com/jobdesc.html?postId=12345"))
check("Meituan detail ID URL recognized", is_direct_detail_url("https://zhaopin.meituan.com/web/position/detail?jobUnionId=4683995941&highlightType=campus"))
check("HR list without jobId rejected", not is_direct_detail_url("https://www.nowcoder.com/jobs/hr/31842"))
check("generic official list rejected", not is_direct_detail_url("https://career.huawei.com/cn/campus-recruitment-job-list"))

# Public list discovery expands only within allowed recruitment-site domains.
check("YJS related public category subdomain is allowed",
      same_host("https://www.yingjiesheng.com/major/", "https://zhiwei.yingjiesheng.com/major/"))
check("51job public recruitment subdomain links are allowed",
      same_host("https://xy.51job.com/", "https://campus.51job.com/jobs/list.html"))
check("Guopin public recruitment subdomain links are allowed",
      same_host("https://job.iguopin.com/", "https://www.iguopin.com/joblist?page=2"))
check("51job public listing links are discoverable",
      should_discover_public_url("51job", "https://campus.51job.com/jobs/list.html"))
check("Guopin public list links are discoverable",
      should_discover_public_url("iguopin", "https://www.iguopin.com/joblist?page=2"))
check("YJS external redirect wrappers are rejected",
      not should_discover_public_url("yingjiesheng", "https://q.yingjiesheng.com/thirdlink?url=https%3A%2F%2Fexample.com%2Fjobs"))
check("public listing rotation advances through known URLs",
      select_rotation_batch(["https://www.nowcoder.com/jobs/page/1","https://www.nowcoder.com/jobs/page/2","https://www.nowcoder.com/jobs/page/3"],0,2)
      == (["https://www.nowcoder.com/jobs/page/1","https://www.nowcoder.com/jobs/page/2"],2))
check("public listing rotation wraps without generating URLs",
      select_rotation_batch(["https://www.nowcoder.com/jobs/page/1","https://www.nowcoder.com/jobs/page/2","https://www.nowcoder.com/jobs/page/3"],2,2)
      == (["https://www.nowcoder.com/jobs/page/3","https://www.nowcoder.com/jobs/page/1"],4))
check("source diagnostics capture public link allowlist decisions",
      '"link_samples"' in open(__file__.replace("test_job_quality.py", "collect_public_sources.py"), encoding="utf-8").read())
check("QZW detail text yields concrete roles",
      extract_qiuzhaowang_role_names("招聘岗位 销售管培生 · 操作管培生 · 客服管培生专业不限 工作城市 全国 面向届次 2027届")
      == ["销售管培生","操作管培生","客服管培生专业不限"])
check("historical QZW composite role rows are purged while announcements survive",
      all(should_purge_qzw_noise_record({"source":"秋招网公开聚合","granularity":"review","title":title}) for title in [
        "AI战略顾问AI应用工程师 消费者营销顾问",
        "投标岗客户管理岗投资岗采购岗",
        "供应链运营岗食安岗",
        "财务岗人力资源岗政岗经营支持岗营运管理岗客户成功岗",
        "市场营销类物理管培类物流运营类海外管培生",
        "品牌营销 社交媒体",
        "研发岗位 开发岗位",
        "算法工程师硬件工程师",
        "软件工程师AI应用工程师 国际营销",
        "研发生产职能市场推广"
      ])
      and not should_purge_qzw_noise_record({"source":"秋招网公开聚合","granularity":"review","title":"销售管培生专业不限"})
      and not should_purge_qzw_noise_record({"source":"秋招网公开聚合","granularity":"announcement","title":"2027届秋季招聘公告"}))
check("QZW mixed-role card titles are rejected as a single vacancy",
      all(not is_public_role_lead_title(title,"qiuzhaowang") for title in [
        "AI战略顾问AI应用工程师 消费者营销顾问",
        "投标岗客户管理岗投资岗采购岗",
        "供应链运营岗食安岗",
        "财务岗人力资源岗政岗经营支持岗营运管理岗客户成功岗",
        "市场营销类物理管培类物流运营类海外管培生",
        "品牌营销 社交媒体",
        "研发岗位 开发岗位"
      ]))
check("QZW role extractor drops concatenated taxonomy and duplicate-ending titles",
      extract_qiuzhaowang_role_names("招聘岗位 投标岗客户管理岗投资岗采购岗 工作城市 全国 面向届次 2027届")==[]
      and extract_qiuzhaowang_role_names("招聘岗位 市场营销类物理管培类物流运营类海外管培生 工作城市 全国 面向届次 2027届")==[]
      and extract_qiuzhaowang_role_names("招聘岗位 算法工程师硬件工程师 工作城市 全国 面向届次 2027届")==[]
      and extract_qiuzhaowang_role_names("招聘岗位 研发生产职能市场推广 工作城市 全国 面向届次 2027届")==[])
check("QZW listing hint yields concrete roles",
      extract_qiuzhaowang_role_names("", "销售管培生 · 操作管培生")
      == ["销售管培生","操作管培生"])

check("YJS list/category page is discoverable",
      should_discover_public_url("yingjiesheng", "https://www.yingjiesheng.com/major/"))
check("YJS public zhuyanye category directory is discoverable",
      should_discover_public_url("yingjiesheng", "https://www.yingjiesheng.com/zhuanye/")
      and 'path.startswith(("/major/","/zhuanye/","/other/","/jobs/","/job/","/joblist/","/xuanjianghui/"))' in COLLECTOR_SOURCE)
check("YJS job-detail page is discoverable",
      should_discover_public_url("yingjiesheng", "https://m.yingjiesheng.com/jobdetail/173508261"))
check("YJS unrelated host is rejected",
      not should_discover_public_url("yingjiesheng", "https://example.com/jobdetail/173508261"))
check("BOSS public campus page is discoverable",
      should_discover_public_url("boss", "https://www.zhipin.com/school/"))
check("BOSS public school hub is eligible for anonymous category rendering",
      should_render_public_page("boss","https://www.zhipin.com/school/","<html><body><div id='app'>public</div></body></html>")
      and 'sourceId === "boss"' in RENDERER_SOURCE)
check("51job public tab selector keeps a whitespace-aware regex",
      "const tabPattern = /^\\s*(校园招聘|招聘岗位|招聘职位|职位列表|岗位列表|职位信息|岗位信息|职位介绍|职位)\\s*$/;" in RENDERER_SOURCE)
check("BOSS category renderer has bounded role-category labels",
      all(label in RENDERER_SOURCE for label in ("AI","算法","互联网","研发","产品","运营","管培生","国企","银行","教师","外企")))
check("BOSS generic consumer search is not browser-rendered",
      not should_render_public_page("boss","https://www.zhipin.com/web/geek/job?query=后端开发","<html><body>搜索</body></html>"))
check("BOSS consumer search page is not treated as a campus listing",
      not should_discover_public_url("boss", "https://www.zhipin.com/web/geek/job?query=后端开发"))
check("BOSS consumer category query is not a direct vacancy",
      not is_direct_detail_url("https://www.zhipin.com/web/geek/job?experience=102&query=后端开发"))
check("BOSS public detail URL remains discoverable",
      should_discover_public_url("boss", "https://www.zhipin.com/job_detail/abc123.html"))
check("diagnostic prioritizes direct BOSS vacancy URLs over category searches",
      diagnostic_link_priority("boss","https://www.zhipin.com/job_detail/abc123.html","后端开发")
      < diagnostic_link_priority("boss","https://www.zhipin.com/web/geek/job?query=后端开发","后端开发"))
check("diagnostic drops ordinary navigation links",
      diagnostic_link_priority("boss","https://www.zhipin.com/about/","关于我们")>=9)
check("BOSS category search routes rank as non-list leads",
      diagnostic_link_priority("boss","https://www.zhipin.com/web/geek/job?query=后端开发","后端开发")>=2)
check("diagnostic keeps allowed public listing routes",
      diagnostic_link_priority("51job","https://campus.51job.com/jobs/list.html","校园招聘职位列表")<9)

check("BOSS public detail route is recognized",
      is_direct_detail_url("https://www.zhipin.com/job_detail/abc123.html"))
check("BOSS link parser enumerates links before using role index",
      'for link_index,a in enumerate(p.links):\n            title=clean_job_title(a["text"],src.get("id",""))' in COLLECTOR_SOURCE)
check("BOSS source no longer queues its generic /web/geek/job search route",
      'path.startswith("/web/geek/job")' not in COLLECTOR_SOURCE)
check("Guopin public campus list is discoverable",
      should_discover_public_url("iguopin","https://job.iguopin.com/jobList?channel=campus"))
check("Guopin jobList is not a direct vacancy",
      not is_direct_detail_url("https://job.iguopin.com/jobList?channel=campus"))
check("Guopin detail needs a job id",
      is_direct_detail_url("https://job.iguopin.com/jobDetail?jobId=78321"))
check("51job campus homepage is discoverable",
      should_discover_public_url("51job","https://xy.51job.com/"))
check("51job numeric vacancy detail is recognized",
      is_direct_detail_url("https://jobs.51job.com/shanghai-pdx/160193763.html"))
check("51job category root is not a detail",
      not is_direct_detail_url("https://xy.51job.com/"))
check("Guopin public SPA route uses anonymous browser rendering",
      should_render_public_page("iguopin","https://job.iguopin.com/jobList?channel=campus","<html><body><div id='app'></div></body></html>"))
check("Guopin detailed campus list parameters remain public-renderable",
      should_render_public_page("iguopin","https://job.iguopin.com/jobList?campus_nature=&channel=campus&nature_cn=","<html><body><div id='app'></div></body></html>"))
check("51job renderer upgrades all allow-listed internal HTTP subdomains before browser launch",
      'if source_id=="51job" and urlparse(url).scheme.lower()=="http" and same_host("https://51job.com/",url)' in COLLECTOR_SOURCE)
check("51job campus microsite HTTP links are in the public rendering allow-list",
      should_render_public_page("51job","http://campus.51job.com/asiainfo/jobs.html","<html><body><a href='javascript:;'>岗位列表</a></body></html>"))
check("QZW partial-read fallback is implemented",
      "except IncompleteRead as exc" in COLLECTOR_SOURCE)
check("renderer reports visible body preview and safe campus navigation",
      "body_preview" in RENDERER_SOURCE and "招聘岗位|招聘职位|职位列表" in RENDERER_SOURCE)
constReg=json.loads((Path(__file__).resolve().parents[1]/"data/company_sources.json").read_text(encoding="utf-8"))
check("Guopin current public www job-list route configured",
      any("https://www.iguopin.com/job/" == str(u) for src in constReg if src.get("id")=="iguopin" for u in src.get("seed_urls",[])))
check("Guopin public keyword category sources configured on live list route",
      any("/job/list?keyword=%E5%B7%A5%E7%A8%8B%E5%B8%88" in str(u) for src in constReg if src.get("id")=="iguopin" for u in src.get("seed_urls",[])))
check("Guopin public job-list SPA routes are browser-renderable",
      should_render_public_page("iguopin","https://www.iguopin.com/job/list?keyword=%E5%B7%A5%E7%A8%8B%E5%B8%88","<html><body><div id='app'></div></body></html>")
      and "job\\/list" in RENDERER_SOURCE and "job(?:\\/|$)" in RENDERER_SOURCE)
check("Guopin base job list route is browser-renderable",
      should_render_public_page("iguopin","https://www.iguopin.com/job/","<html><body><div id='app'></div></body></html>"))
check("Guopin job detail route with an id query is recognized",
      is_direct_detail_url("https://www.iguopin.com/job/detail?id=208162873677972376"))
check("Guopin visible role cards become pending leads, not priority jobs",
      (lambda p: (p.feed('<html><title>招聘信息-国聘</title><body><span>互联网运营</span><span>西安-未央区</span><span>面议校招经验不限本科</span><span>不限</span><span>中国移动通信有限公司在线营销服务中心</span><span>国企</span></body></html>'),
        (lambda r: len(r)==1 and r[0].get("title")=="互联网运营" and r[0].get("company")=="中国移动通信有限公司在线营销服务中心"
          and r[0].get("granularity")=="lead" and r[0].get("status")=="pending_review"
          and should_publish_record(r[0]) and not assess(dict(r[0])))(extract_visible_public_role_leads("iguopin",p,"https://www.iguopin.com/job/list?keyword=AI","国聘公开招聘",p.page_title))))(Parser()))
check("public-list lead title filter rejects job-description labels, numbered bullets and industry taxonomy",
      all(not is_public_role_lead_title(title,source_id) for title,source_id in [
        ("工作内容：","51job"),
        ("1、根据需求，制定模块的spec，完成前端设计和相关调试工作。","51job"),
        ("3、负责电路设计spec及设计文档撰写，电路分析与仿真验证，电路模型支持，测试与debug分析；","51job"),
        ("广播、电视、电影和录音制作业","iguopin"),
        ("智能汽车制造产业链专项引才计划","iguopin"),
        ("海南蜈支洲旅游开发股份有限公司三亚蜈支洲岛旅游区","iguopin"),
        ("财务岗人力资源岗政岗经营支持岗营运管理岗客户成功岗","qiuzhaowang"),
        ("研发岗算法岗产品岗运营岗位技术岗","qiuzhaowang"),
        ("算法工程师硬件工程师","qiuzhaowang"),
        ("软件工程师AI应用工程师 国际营销","qiuzhaowang"),
        ("研发生产职能市场推广","qiuzhaowang"),
        ("IC设计","51job"),
        ("我是亚亚，欢迎咨询","51job")
      ]))
check("public-list lead title filter retains concrete role names",
      all(is_public_role_lead_title(title,source_id) for title,source_id in [
        ("AI大模型开发工程师","51job"),
        ("行政专员/助理","iguopin"),
        ("AI触点运营","51job"),
        ("AI开发岗（AI应用）-金种子（深圳）","iguopin"),
        ("软件产品经理","iguopin"),
        ("混合信号设计工程师","51job"),
        ("硬件工程师 -(PCB 设计)","qiuzhaowang")
      ]))
check("public-list leads dedupe across query URLs but preserve city distinction",
      public_lead_dedupe_key({"source":"国聘公开招聘","company":"某科技有限公司","title":"行政专员","city":"上海","source_url":"https://www.iguopin.com/job/list?keyword=行政"})
      == public_lead_dedupe_key({"source":"国聘公开招聘","company":"某科技有限公司","title":"行政专员","city":"上海","source_url":"https://www.iguopin.com/job/list?keyword=运营"})
      and public_lead_dedupe_key({"source":"国聘公开招聘","company":"某科技有限公司","title":"行政专员","city":"北京"})
      != public_lead_dedupe_key({"source":"国聘公开招聘","company":"某科技有限公司","title":"行政专员","city":"上海"}))
check("source metrics distinguish verified details from list/card leads",
      source_role_metrics([
        {"source_url":"https://www.zhipin.com/job_detail/abc123.html","verification_status":"unknown",
         "granularity":"review","public_card_lead":True},
        {"source_url":"https://www.iguopin.com/job/list?keyword=AI","verification_status":"unknown",
         "granularity":"lead","lead_evidence":"visible_public_job_card"},
        {"source_url":"https://www.nowcoder.com/jobs/detail/462503","verification_status":"link_alive",
         "granularity":"job"}
      ])=={"public_list_leads":1,"public_card_leads":1,"direct_detail_role_records":1})
check("BOSS public card is not counted as parsed detail even if a link check passes",
      source_role_metrics([
        {"source_url":"https://www.zhipin.com/job_detail/abc123.html","verification_status":"link_alive",
         "granularity":"review","public_card_lead":True}
      ])["direct_detail_role_records"]==0)
check("contact/title noise cannot be published as a list lead",
      not should_publish_record({"source":"前程无忧校园招聘公开岗位","granularity":"lead","lead_evidence":"visible_public_job_card",
        "status":"pending_review","company":"亚信科技有限公司","title":"我是亚亚，欢迎咨询",
        "source_url":"https://campus.51job.com/asiainfo/jobs.html","source_card_text":"我是亚亚，欢迎咨询",
        "source_page_title":"亚信2027届校园招聘"}))
check("old-cycle Guopin lead cannot enter the public pending database",
      not should_publish_record({"source":"国聘公开招聘","granularity":"lead","lead_evidence":"visible_public_job_card",
        "status":"pending_review","company":"某科技有限公司","title":"软件开发工程师",
        "source_url":"https://www.iguopin.com/job/list?keyword=软件",
        "source_card_text":"软件开发工程师 2026届校招 本科 某科技有限公司",
        "source_page_title":"招聘信息-国聘","cohort":"2026届","cohort_evidence":"page_text"}))
check("51job public 2027 campus role list can yield a clearly tagged review lead",
      (lambda p: (p.feed('<html><title>亚信2027届校园招聘</title><body><span>职位名称</span><span>AI大模型开发工程师</span><span>北京</span><span>应届生本科</span><span>亚信科技有限公司</span></body></html>'),
        (lambda r: len(r)==1 and r[0].get("company")=="亚信科技有限公司" and r[0].get("cohort")=="2027届"
          and r[0].get("granularity")=="lead" and should_publish_record(r[0]) and not assess(dict(r[0])))(extract_visible_public_role_leads("51job",p,"https://campus.51job.com/asiainfo/jobs.html","前程无忧校园招聘公开岗位",p.page_title))))(Parser()))

check("51job root with no public detail links uses render fallback",
      should_render_public_page("51job","https://xy.51job.com/","<html><body><a href='javascript:;'>校园招聘</a></body></html>"))
check("51job page with real detail link skips redundant rendering",
      not should_render_public_page("51job","https://xy.51job.com/","<a href='https://jobs.51job.com/shanghai-pdx/160193763.html'>软件工程师</a>"))
check("YJS uses static parsing rather than unnecessary browser rendering",
      not should_render_public_page("yingjiesheng","https://m.yingjiesheng.com/other/index.html","<html><body>岗位列表</body></html>"))
check("Guopin login route is not browser-rendered",
      not should_render_public_page("iguopin","https://job.iguopin.com/login","<html></html>"))
check("browser worker has explicit public host allow-list and no CAPTCHA bypass",
      "security_interstitial: security" in RENDERER_SOURCE and "allowPublicUrl" in RENDERER_SOURCE and "captcha.solve" not in RENDERER_SOURCE)

check("Nowcoder public listing route is discoverable",
      should_discover_public_url("nowcoder", "https://www.nowcoder.com/jobs/recommend/campus"))
check("broad major-only strings are not concrete roles",
      not is_specific_role_title("商务英语"))
check("specific role with qualification suffix remains a role",
      is_specific_role_title("销售管培生专业不限"))
check("bare generic engineer title is rejected for job granularity",
      not is_specific_role_title("工程师"))
check("bare generic designer title is rejected for job granularity",
      not is_specific_role_title("设计"))
check("specific engineer title remains accepted",
      is_specific_role_title("研发工程师"))
check("multi-role YJS detail headers split into distinct titles",
      split_role_title_candidates("电气设计|软件设计","yingjiesheng")==["电气设计","软件设计"])
check("YJS province wrapper is removed from the company name",
      clean_yingjiesheng_company_name("[广东]珠海市龙图光罩科技有限公司")=="珠海市龙图光罩科技有限公司")
check("explicit 2025 BOSS campus cards are rejected",
      not is_boss_campus_card_relevant("〖2025华为校园招聘〗软开/AI/算法工程师 北京 在校/应届 本科"))
check("explicit 26-year BOSS graduate cards are rejected",
      not is_boss_campus_card_relevant("新能源软件产品经理-26年应届生"))
check("specific 2027 BOSS card remains accepted",
      is_boss_campus_card_relevant("2027-硬件工程师-北京 北京 在校/应届 本科"))
check("non-graduate lifestyle/side-job BOSS card is rejected",
      not is_boss_campus_card_relevant("便利店店员家门口上班 工作不累 薪资高 经验不限 初中及以下"))
check("BOSS access-wall detail requests are skipped while other source details remain queued",
      not should_queue_detail("boss","https://www.zhipin.com/job_detail/demo.html")
      and should_queue_detail("nowcoder","https://www.nowcoder.com/jobs/detail/462503"))
check("QZW generic majors excluded from published jobs",
      not should_publish_record({"source":"秋招网公开聚合","title":"商务英语","granularity":"review"}))
check("QZW real role title remains publishable when detail URL is present",
      should_publish_record({"source":"秋招网公开聚合","title":"销售管培生专业不限","source_url":"https://qiuzhaowang.com/jobs/position-32406","granularity":"review"}))
check("official recruitment entry leads are retained separately",
      should_publish_record({"source":"秋招网公开聚合","title":"2027届秋招网官方招聘入口线索","granularity":"entry"}))
check("YJS specific role with direct detail is not caught by QZW-only filter",
      should_publish_record({"source":"应届生求职网公开岗位","title":"算法工程师","source_url":"https://m.yingjiesheng.com/jobdetail/173508261","granularity":"review"}))
check("BOSS category query is removed from publication",
      not should_publish_record({"source":"BOSS直聘公开校招岗位","title":"后端开发","source_url":"https://www.zhipin.com/web/geek/job?experience=102&query=后端开发","granularity":"review"}))
check("old BOSS 26-year graduate lead is removed from published database",
      not should_publish_record({"source":"BOSS直聘公开校招卡片","granularity":"job",
        "title":"新能源软件产品经理-26年应届生",
        "source_url":"https://www.zhipin.com/job_detail/abc123.html"}))
check("already-cleaned BOSS title with old 2026 cohort is removed",
      not should_publish_record({"source":"BOSS直聘公开校招卡片","granularity":"job",
        "title":"新能源软件产品经理","cohort":"2026届",
        "source_url":"https://www.zhipin.com/job_detail/abc123.html"}))
check("current BOSS 2027 graduate lead remains eligible for publication",
      should_publish_record({"source":"BOSS直聘公开校招卡片","granularity":"job",
        "title":"2027-硬件工程师-北京 北京 在校/应届 本科",
        "source_url":"https://www.zhipin.com/job_detail/abc123.html"}))
check("Nowcoder non-detail company card is removed from publication",
      not should_publish_record({"source":"牛客公开岗位","title":"重庆千里科技股份有限公司 机械制造 100-499人","source_url":"https://www.nowcoder.com/enterprise/30417?channel=npJobTab","granularity":"review"}))
check("YJS marketing page is removed from publication",
      not should_publish_record({"source":"应届生求职网公开岗位","title":"做开发工程师 ，挑战高年薪","source_url":"https://q.yingjiesheng.com/jobs/search/开发","granularity":"review"}))
check("QZW actual position detail can pass URL gate",
      should_publish_record({"source":"秋招网公开聚合","title":"销售管培生","source_url":"https://qiuzhaowang.com/jobs/position-32406","granularity":"review"}))
check("QZW platform rollup title is cleaned to empty",
      clean_job_title("2027届最新更新校招岗位｜招聘信息与截止时间｜秋招网","qiuzhaowang")=="")
check("YJS old-style public job detail route is recognized",
      is_direct_detail_url("https://m.yingjiesheng.com/job-008-108-543.html"))
check("YJS mobile public listing path is discoverable",
      should_discover_public_url("yingjiesheng", "https://m.yingjiesheng.com/other/index.html"))
check("YJS search subdomain root is discoverable",
      should_discover_public_url("yingjiesheng", "https://s.yingjiesheng.com/"))
check("YJS old HTTP category URLs are normalized to HTTPS",
      normalize_source_url("yingjiesheng","http://zhiwei.yingjiesheng.com/riyu/tianjin")=="https://zhiwei.yingjiesheng.com/riyu/tianjin")
check("Guopin current public keyword category routes are discoverable",
      should_discover_public_url("iguopin","https://www.iguopin.com/job/list?keyword=%E5%B7%A5%E7%A8%8B%E5%B8%88"))
check("51job company profile pages are rejected as listing candidates",
      not should_discover_public_url("51job","https://jobs.51job.com/all/co114386.html"))
check("51job employer campus microsites remain discoverable",
      should_discover_public_url("51job","https://campus.51job.com/coach2027/index.html"))
check("Unicode paths are percent-encoded before requests",
      "%E5" in canonical("https://m.yingjiesheng.com/地区招聘/index.html"))
check("common BOSS major-directory cards are excluded",
      not is_specific_role_title("戏剧与影视学") and not is_specific_role_title("安全科学与工程") and not is_specific_role_title("电气工程"))
check("BOSS and Nowcoder marketing cards are excluded",
      not is_specific_role_title("收藏 快手 全球短视频与直播龙头企业，全生态布局，内容与技术能力领先。 27届秋招：2天截止 地点：上海 立即投递","nowcoder"))
check("BOSS source title drops year/location/degree metadata",
      clean_job_title("2027-硬件工程师-北京 北京 在校/应届 本科 通信相关专业 硬件开发经验","boss")=="硬件工程师")
check("BOSS role with promotional prefix and suffix is cleaned",
      clean_job_title("校招末班车-产品经理-多赛道发展+高管直带 北京 在校/应届 本科 B端产品","boss")=="产品经理")
check("Nowcoder salary/HR metadata is removed from title",
      clean_job_title("收藏 Java(校招岗位)（实习） 200-500元/天 助力简历加分 HR刚处理简历 广州 5天/周","nowcoder")=="Java(校招岗位)（实习）")
check("QZW position URL recognized as direct detail",
      is_direct_detail_url("https://qiuzhaowang.com/jobs/position-32406"))
nc_fields=extract_nowcoder_fields(
    "算法工程师(西安)_诺瓦星云校招_牛客网", ["算法工程师(西安)"],
    "公司名称：诺瓦星云 工作地点：西安 本科 2027届校招")
check("Nowcoder detail company extracted", nc_fields.get("company")=="诺瓦星云")
check("Nowcoder detail title stays clean", nc_fields.get("title")=="算法工程师(西安)")
check("BOSS search-category path is not a direct vacancy",
      not is_direct_detail_url("https://www.zhipin.com/web/geek/job?experience=102&query=后端开发"))
boss_fields=extract_boss_fields(
    "AIGC设计师招聘_上海某某科技有限公司招聘信息-BOSS直聘",
    ["AIGC设计师"], "上海 本科 工作职责：制作AIGC内容。")
check("BOSS detail title extracted from public heading", boss_fields.get("title")=="AIGC设计师")
check("BOSS company extracted from public title", boss_fields.get("company")=="上海某某科技有限公司")
boss_card_links=[
    {"href":"/job_detail/0f452ccba3890a940nF92NW0E1dW.html","text":"27届校招数据安全研究员（博士后） 北京 在校/应届 博士 Python 计算机/信息安全相关专业"},
    {"href":"/gongsi/demo.html","text":"具深智能 机器人"},
]
boss_card=extract_boss_card_fields(
    boss_card_links[0]["text"],boss_card_links,0,"https://www.zhipin.com/school/")
check("BOSS public card title is cleaned",boss_card.get("title")=="数据安全研究员（博士后）")
check("BOSS public card company is inferred from adjacent visible company link",boss_card.get("company")=="具深智能")
check("BOSS public card salary/degree/location metadata remains extractable",
      boss_card.get("city")=="北京" and boss_card.get("degree")=="博士")
check("BOSS public card explicitly shows 2027 cohort",boss_card.get("cohort")=="2027届")
check("BOSS job_detail route is directly verifiable as a URL type",
      verifier_is_direct_detail("https://www.zhipin.com/job_detail/0f452ccba3890a940nF92NW0E1dW.html"))
check("BOSS security interstitial URL is not a verified vacancy",
      is_security_interstitial_url("https://www.zhipin.com/web/passport/zp/security.html?callbackUrl=/job_detail/demo.html"))


# Parse visible fields on a public YJS detail-page fixture; never invent absent values.
fixture_title = "AIGC影视创意制作（校招）招聘_上海基美影业股份有限公司招聘信息-应届生求职网"
fixture_text = ("AIGC影视创意制作（校招） 8千-1.5万 本科 全职 工作地点：上海-静安区 "
                "职位描述 岗位职责：生成式AI视频创意。岗位要求：熟悉AIGC工具。"
                "公司信息 上海基美影业股份有限公司 公司地址：上海静安区某路1号")
fields = extract_yingjiesheng_fields(
    fixture_title, fixture_text, "https://m.yingjiesheng.com/jobdetail/170935981"
)
check("AIGC title accepted as a concrete role", is_concrete_role_title("AIGC影视创意制作"))
check("YJS detail role title extracted", fields.get("title") == "AIGC影视创意制作（校招）")
check("YJS salary extracted", fields.get("salary") == "8千-1.5万")
check("YJS degree extracted", fields.get("degree") == "本科")
check("YJS city extracted", fields.get("city") == "上海")
check("YJS company extracted", fields.get("company") == "上海基美影业股份有限公司")
check("YJS responsibilities extracted", "生成式AI视频创意" in fields.get("responsibilities", ""))
check("YJS requirements extracted", "AIGC工具" in fields.get("requirements", ""))
check("YJS address extracted", fields.get("address") == "上海静安区某路1号")
legacy_yjs=extract_yingjiesheng_fields(
    "[广东]珠海市龙图光罩科技有限公司 品质工程师 2026-10-10",
    "工作地点：珠海市 招聘岗位：品质工程师 职位描述：质量检验与分析。学历要求：本科",
    "https://m.yingjiesheng.com/job-008-111-850.html")
check("YJS old mobile title yields a clean role name",
      legacy_yjs.get("title")=="品质工程师")
check("YJS old mobile title yields the company",
      legacy_yjs.get("company")=="珠海市龙图光罩科技有限公司")
check("YJS old mobile title's date is only a publication date",
      legacy_yjs.get("publish_date")=="2026-10-10" and legacy_yjs.get("cohort","")=="")
check("YJS explicit Other work location is not inferred as Shanghai",
      extract_yingjiesheng_fields(
        "[广东]珠海市龙图光罩科技有限公司 品质工程师 2026-10-10",
        "工作地点：其它 信息来源：广州大学 职位类型：全职 上海北京广州导航",
        "https://m.yingjiesheng.com/job-008-111-850.html").get("city")=="其它")
check("YJS explicit work city overrides site-navigation cities",
      extract_yingjiesheng_fields(
        "[广东]珠海市龙图光罩科技有限公司 品质工程师 2026-10-10",
        "工作地点：珠海市 信息来源：广州大学 职位类型：全职 上海北京导航",
        "https://m.yingjiesheng.com/job-008-111-850.html").get("city")=="珠海")
check("YJS salary parser reads RMB monthly ranges",
      extract_yingjiesheng_fields(
        "品质工程师招聘_企业招聘信息",
        "职位：品质工程师 发布时间：2026-10-10 工作地点：其它 5000～6000元/月",
        "https://m.yingjiesheng.com/jobdetail/170935981").get("salary")=="5000～6000元/月")


annual_fields=extract_yingjiesheng_fields(
    "苏州艾科瑞思智能装备股份有限公司招聘AIGC开发工程师_苏州校园招聘",
    "职位：AIGC开发工程师 发布时间：2026-10-09 工作地点：苏州 学历要求：硕士 职位描述：开发AI应用 18万元/年",
    "https://m.yingjiesheng.com/job-008-108-543.html")
check("YJS annual salary is not mistaken for monthly salary",
      annual_fields.get("salary") == "18万元/年")
check("YJS details keep Suzhou available in city extraction",
      "苏州" in extract_detail_fields_generic("工作城市：江苏省苏州市苏州工业园区").get("city",""))
multi_city = extract_detail_fields_generic("2027届 AIGC算法工程师 工作地点：北京市,上海市 本科及以上 任职要求：熟悉多模态生成。")
check("multi-city detail keeps Shanghai filterable",
      multi_city.get("city") == "北京、上海")
inline_yjs = extract_detail_fields_generic(
    "全国[切换]首页 职位 公司 校招 北京 上海 广州 深圳 AIGC影视创意制作（校招） "
    "8千-1.5万 本科 全职 影视动画、新媒体技术 上海-静安区 王女士 主管·刚刚活跃 职位亮点 年终奖金",
    "AIGC影视创意制作（校招）")
check("generic detail city ignores navigation and reads inline job metadata",
      inline_yjs.get("city") == "上海")
labelled_city = extract_detail_fields_generic(
    "全国[切换]首页 北京 上海 广州 职位名称：后端开发工程师 "
    "工作地点：成都高新区 信息来源：北京 上海 导航信息 学历要求：本科")
check("labelled work location overrides every navigation city",
      labelled_city.get("city") == "成都")
national_city = extract_detail_fields_generic(
    "AI产品经理 15K 本科 全职 工作地点：全国 职位描述：服务多个城市")
check("explicit nationwide location is retained",
      national_city.get("city") == "全国")
description_only_city = extract_detail_fields_generic(
    "AI算法工程师 20K 本科 全职 岗位职责：负责上海、北京客户项目；岗位要求：熟悉模型开发。")
check("cities mentioned only in responsibilities are not treated as work locations",
      "city" not in description_only_city)


base = {
    "id": "fixture-priority",
    "company": "上海基美影业股份有限公司",
    "title": "AIGC影视创意制作",
    "source": "应届生求职网公开岗位",
    "source_url": "https://m.yingjiesheng.com/jobdetail/170935981",
    "status": "active",
    "cohort": "2027届",
    "cohort_evidence": "page_text",
    "cohort_confirmed": True,
    "verification_status": "link_alive",
    "verification_http_status": 200,
    "last_verified": "2026-10-09",
    "last_seen": "2026-10-10",
    "last_collected": "2026-10-10",
    "last_verified_at": "2026-10-09T12:00:00+00:00",
    "deadline": "2026-12-31",
    "collector": "public-html-v4",
    "granularity": "job",
}
good = dict(base)
check("specific verified-cohort detail can be prioritized", assess(good, today=date(2026, 10, 10)))
generic = dict(base, source_url="https://www.yingjiesheng.com/")
check("generic portal downgraded", not assess(generic, today=date(2026, 10, 10)))
missing_cohort = dict(base, cohort="", cohort_evidence="", cohort_confirmed=False)
check("missing cohort downgraded", not assess(missing_cohort, today=date(2026, 10, 10)))

unproven_cohort = dict(base, cohort="2027届", cohort_evidence="", cohort_confirmed=False)
check("a 2027 label without visible evidence is downgraded", not assess(unproven_cohort, today=date(2026, 10, 10)))
registry_only = dict(base, cohort_evidence="source_registry", cohort_confirmed=True)
check("site registry cohort alone is not proof", not assess(registry_only, today=date(2026, 10, 10)))
stale_discovery = dict(base, last_seen="2026-09-25", last_collected="2026-09-25")
check("job not rediscovered for 14 days is downgraded", not assess(stale_discovery, today=date(2026, 10, 10)))
stale = dict(base, verification_status="stale")
check("stale detail downgraded", not assess(stale, today=date(2026, 10, 10)))
check("lead bucketing does not destroy lifecycle status", stale["status"] == "active" and stale["review_bucket"] == "lead")
old_v3 = dict(base, collector="public-html-v3", cohort_evidence="", cohort_confirmed=False)
check("legacy default 2027 does not count as evidence", not assess(old_v3, today=date(2026, 10, 10)))
expired = dict(base, deadline="2026-10-09")
check("expired job is not prioritized", not assess(expired, today=date(2026, 10, 10)) and expired["status"] == "expired")

compact_sample = [{"company": "上海基美影业", "title": "AIGC影视创意制作", "city": "上海"}]
compact_payload = compact_json(compact_sample)
check("published JSON serialization stays compact and round-trips",
      "\\n" not in compact_payload and ": " not in compact_payload
      and ", " not in compact_payload and json.loads(compact_payload) == compact_sample)

print("QUALITY REGRESSION TESTS PASSED")
