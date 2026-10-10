"""Single source of truth for deciding which job records deserve priority review."""
import json
from datetime import date, timedelta
from collect_public_sources import clean_job_title, is_concrete_role_title, is_direct_detail_url, sanitize_extracted_text

# Only explicit text evidence is accepted; a site-level registry label cannot prove
# that each linked vacancy belongs to the 2027 graduate cohort.
COHORT_EVIDENCE_OK = {"page_text", "visible_announcement_title", "job_detail_title", "job_detail_text"}

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
    source_id = "nowcoder" if source == "牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in source else "")
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
