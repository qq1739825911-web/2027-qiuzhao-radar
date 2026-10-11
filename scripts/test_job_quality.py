"""Offline regression tests for public job parsing and quality buckets."""
import json
from datetime import date
from collect_public_sources import (
    year, clean_job_title, is_direct_detail_url, is_concrete_role_title,
    extract_yingjiesheng_fields, extract_detail_fields_generic, Parser, sanitize_extracted_text,
    same_host, should_discover_public_url, is_specific_role_title, extract_boss_fields, extract_nowcoder_fields, canonical,
)
from quality_rules import assess, compact_json, should_publish_record

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
check("YJS list/category page is discoverable",
      should_discover_public_url("yingjiesheng", "https://www.yingjiesheng.com/major/"))
check("YJS job-detail page is discoverable",
      should_discover_public_url("yingjiesheng", "https://m.yingjiesheng.com/jobdetail/173508261"))
check("YJS unrelated host is rejected",
      not should_discover_public_url("yingjiesheng", "https://example.com/jobdetail/173508261"))
check("BOSS public campus page is discoverable",
      should_discover_public_url("boss", "https://www.zhipin.com/school/"))
check("BOSS public detail route is recognized",
      is_direct_detail_url("https://www.zhipin.com/job_detail/abc123.html"))
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
check("Nowcoder public listing route is discoverable",
      should_discover_public_url("nowcoder", "https://www.nowcoder.com/jobs/recommend/campus"))
check("broad major-only strings are not concrete roles",
      not is_specific_role_title("商务英语"))
check("specific role with qualification suffix remains a role",
      is_specific_role_title("销售管培生专业不限"))
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
check("Unicode paths are percent-encoded before requests",
      "%E5" in canonical("https://m.yingjiesheng.com/地区招聘/index.html"))
check("common BOSS major-directory cards are excluded",
      not is_specific_role_title("戏剧与影视学") and not is_specific_role_title("安全科学与工程") and not is_specific_role_title("电气工程"))
check("BOSS and Nowcoder marketing cards are excluded",
      not is_specific_role_title("收藏 快手 全球短视频与直播龙头企业，全生态布局，内容与技术能力领先。 27届秋招：2天截止 地点：上海 立即投递","nowcoder"))
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

# Parse visible fields on a public YJS detail-page fixture; never invent absent values.
fixture_title = "AIGC影视创意制作（校招）招聘_上海基美影业股份有限公司招聘信息-应届生求职网"
fixture_text = ("AIGC影视创意制作（校招） 8千-1.5万 本科 全职 上海-静安区 "
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
