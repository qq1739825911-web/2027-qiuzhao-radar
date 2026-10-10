"""Offline regression tests for public job parsing and quality buckets."""
import json
from datetime import date
from collect_public_sources import (
    year, clean_job_title, is_direct_detail_url, is_concrete_role_title,
    extract_yingjiesheng_fields,
)
from quality_rules import assess, compact_json

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

# Direct URLs only; aggregator/search/list pages are not job details.
check("YJS numeric job detail recognized", is_direct_detail_url("https://m.yingjiesheng.com/jobdetail/170935981"))
check("Nowcoder detail recognized", is_direct_detail_url("https://www.nowcoder.com/jobs/detail/462503"))
check("Baidu uuid direct URL recognized", is_direct_detail_url("https://talent.baidu.com/jobs/detail/GRADUATE/a20dfb76-4c9d-4814-bd74-7561616e2773"))
check("Tencent detail post URL recognized", is_direct_detail_url("https://careers.tencent.com/jobdesc.html?postId=12345"))
check("Meituan detail ID URL recognized", is_direct_detail_url("https://zhaopin.meituan.com/web/position/detail?jobUnionId=4683995941&highlightType=campus"))
check("HR list without jobId rejected", not is_direct_detail_url("https://www.nowcoder.com/jobs/hr/31842"))
check("generic official list rejected", not is_direct_detail_url("https://career.huawei.com/cn/campus-recruitment-job-list"))

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
