"""Single source of truth for deciding which job records deserve priority review."""
import json
import re
from datetime import date, timedelta
from collect_public_sources import clean_job_title, is_concrete_role_title, is_specific_role_title, is_direct_detail_url, sanitize_extracted_text, is_boss_campus_card_relevant, is_public_role_lead_title

# Only explicit text evidence is accepted; a site-level registry label cannot prove
# that each linked vacancy belongs to the 2027 graduate cohort.
COHORT_EVIDENCE_OK = {"page_text", "visible_announcement_title", "job_detail_title", "job_detail_text"}

def should_publish_record(row):
    """Remove aggregator category junk and stale BOSS cycles; allow only clearly tagged public list leads to remain pending."""
    source=str(row.get("source",""))
    granularity=str(row.get("granularity",""))
    if granularity in {"announcement","entry"}:
        return True
    source_id = "nowcoder" if source == "牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in source else ("qiuzhaowang" if "秋招网" in source else ("boss" if "BOSS直聘" in source else ("iguopin" if "国聘" in source else ("51job" if "前程无忧" in source or "51job" in source.lower() else "")))))
    if source_id in {"nowcoder","yingjiesheng","boss","qiuzhaowang","iguopin","51job"}:
        title=clean_job_title(row.get("title",""),source_id)
        if not is_specific_role_title(title,source_id):
            return False
        if granularity=="lead" and row.get("lead_evidence")=="visible_public_job_card" and not is_public_role_lead_title(title,source_id):
            return False
        if not is_direct_detail_url(row.get("source_url","")):
            # A narrow exception for clearly identified job cards on public Guopin/51job
            # vacancy lists. These rows are explicitly low-confidence leads and cannot
            # become priority because they lack a per-role detail URL and link verification.
            if granularity!="lead" or row.get("lead_evidence")!="visible_public_job_card" or str(row.get("status",""))=="active":
                return False
            company=str(row.get("company","")).strip()
            if not company or company in {"国聘","前程无忧51job","前程无忧","BOSS直聘"}:
                return False
            card=str(row.get("source_card_text",""))
            page_title=str(row.get("source_page_title",""))
            if source_id=="iguopin":
                from urllib.parse import urlparse
                parsed=urlparse(str(row.get("source_url","")))
                host=parsed.netloc.lower()
                if not (host=="www.iguopin.com" or host.endswith(".iguopin.com")):
                    return False
                if parsed.path.rstrip("/")!="/job" and not parsed.path.startswith("/job/list"):
                    return False
                if not re.search(r"(?:校招|应届生|应届|在校生|2027\s*届|27\s*届)",card):
                    return False
                if re.search(r"(?<!\d)(?:202[0-6]|2[0-6])\s*(?:届|年应届|年校招|年校园招聘)",card) and not re.search(r"(?<!\d)2027\s*(?:届|年)?\s*(?:校招|校园招聘|应届)|(?<!\d)27\s*届",card):
                    return False
            elif source_id=="51job":
                from urllib.parse import urlparse
                parsed=urlparse(str(row.get("source_url","")))
                host=parsed.netloc.lower()
                if not host.endswith(".51job.com") or not host.startswith("campus."):
                    return False
                if not re.search(r"2027.*(?:校园招聘|校招)|(?:校园招聘|校招).*2027",page_title):
                    return False
            return True
        # Reapply the current cohort-cycle rule to historical records as well as
        # newly collected BOSS cards, so stale "26年应届" rows don't linger forever.
        if source_id=="boss":
            cohort=str(row.get("cohort","") or "")
            # A legacy record may have had its title cleaned already while its
            # cohort field still identifies the old graduating class.
            cohort_year=re.search(r"(?<!\d)(?:19|20)\d{2}\s*届",cohort)
            if cohort_year and not cohort_year.group(0).startswith("2027"):
                return False
            if not is_boss_campus_card_relevant(row.get("title","")):
                return False
        return True
    return True

def compact_json(payload):
    """Serialize the published job database without indentation to reduce page load size."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def cohort_is_confirmed(row):
    cohort = str(row.get("cohort", "")).strip()
    evidence = str(row.get("cohort_evidence", "")).strip()
    if not cohort.startswith("2027"):
        return False
    return evidence in COHORT_EVIDENCE_OK

def assess(row, today=None):
    today = today or date.today()
    source = str(row.get("source", ""))
    source_id = "nowcoder" if source == "牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in source else ("qiuzhaowang" if "秋招网" in source else ("boss" if "BOSS直聘" in source else ("iguopin" if "国聘" in source else ("51job" if "前程无忧" in source or "51job" in source.lower() else "")))))
    title = clean_job_title(row.get("title", ""), source_id)
    if title:
        row["title"] = title
    # Clean existing historical records too; otherwise the fixed collector
    # would leave already-published JSON fragments visible in the detail modal.
    field_limits = {"description": 6500, "responsibilities": 4500, "requirements": 4500,
                    "benefits": 2000, "company_info": 2000, "address": 240, "work_time": 500}
    for field, limit in field_limits.items():
        value = row.get(field)
        if isinstance(value, str) and value:
            row[field] = sanitize_extracted_text(value, limit)
    reasons = []
    if not title or not is_concrete_role_title(title):
        reasons.append("职位名称不够明确或混有平台/招聘文案")
    if not is_direct_detail_url(row.get("source_url", "")):
        reasons.append("缺少岗位级详情链接")
    if not cohort_is_confirmed(row):
        reasons.append("未能从公开页面证据确认2027届招聘批次")

    status = row.get("status", "pending_review")
    if status in {"closed", "expired", "withdrawn", "cancelled"}:
        reasons.append("岗位已关闭或截止日期已过")
    deadline = str(row.get("deadline", "") or "")
    if deadline:
        try:
            if date.fromisoformat(deadline) < today:
                row["status"] = "expired"
                if "岗位已关闭或截止日期已过" not in reasons:
                    reasons.append("截止日期已过")
        except ValueError:
            reasons.append("截止日期格式异常")

    verification = str(row.get("verification_status", "") or "")
    if verification != "link_alive":
        reasons.append("详情链接尚未通过可访问性核验")
    if verification in {"stale", "missing_recheck", "closed", "unknown"}:
        reasons.append("链接核验过期、失效复查中或暂无法确认")
    last_check = str(row.get("last_verified", "") or "")
    check_cutoff = (today - timedelta(days=7)).isoformat()
    if not last_check or last_check < check_cutoff:
        reasons.append("最近一次链接核验超过7天或缺少核验日期")

    # A link can still load after a vacancy disappeared from its source listing.
    # Require recent rediscovery as well as a recent successful link check.
    last_seen = str(row.get("last_seen") or row.get("last_collected") or "")
    seen_cutoff = (today - timedelta(days=14)).isoformat()
    if not last_seen or last_seen < seen_cutoff:
        reasons.append("招聘来源超过14天未重新发现该岗位")
    if row.get("status") != "active":
        reasons.append("未达到自动优先核验条件")

    ready = not reasons and row.get("status") == "active"
    row["review_bucket"] = "priority" if ready else "lead"
    row["review_reasons"] = [] if ready else list(dict.fromkeys(reasons or ["等待人工核验"]))
    return ready
