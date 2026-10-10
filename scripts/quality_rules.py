"""Single source of truth for deciding which job records deserve priority review."""
from datetime import date, timedelta
from collect_public_sources import clean_job_title, is_concrete_role_title, is_direct_detail_url

def cohort_is_confirmed(row):
    cohort=str(row.get("cohort","")).strip()
    if not cohort.startswith("2027"): return False
    if row.get("cohort_evidence") or row.get("cohort_confirmed"): return True
    # V3 silently defaulted every page to 2027届. Never trust that old default.
    if row.get("collector")=="public-html-v3": return False
    return bool(row.get("cohort"))

def assess(row, today=None):
    today=today or date.today()
    source=str(row.get("source",""))
    source_id="nowcoder" if source=="牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in source else "")
    title=clean_job_title(row.get("title",""),source_id)
    if title: row["title"]=title
    reasons=[]
    if not title or not is_concrete_role_title(title):
        reasons.append("职位名称不够明确或混有平台/招聘文案")
    if not is_direct_detail_url(row.get("source_url","")):
        reasons.append("缺少岗位级详情链接")
    if not cohort_is_confirmed(row):
        reasons.append("未能明确确认2027届招聘批次")
    status=row.get("status","pending_review")
    if status in {"closed","expired","withdrawn","cancelled"}:
        reasons.append("岗位已关闭或截止日期已过")
    deadline=str(row.get("deadline","") or "")
    if deadline:
        try:
            if date.fromisoformat(deadline)<today:
                row["status"]="expired"
                if "岗位已关闭或截止日期已过" not in reasons: reasons.append("截止日期已过")
        except ValueError:
            reasons.append("截止日期格式异常")
    verification=str(row.get("verification_status","") or "")
    if verification!="link_alive":
        reasons.append("详情链接尚未通过可访问性核验")
    if verification in {"stale","missing_recheck","closed","unknown"}:
        reasons.append("链接核验过期、失效复查中或暂无法确认")
    last_check=str(row.get("last_verified","") or "")
    cutoff=(today-timedelta(days=7)).isoformat()
    if not last_check or last_check<cutoff:
        reasons.append("最近一次链接核验超过7天或缺少核验日期")
    if row.get("status")!="active":
        reasons.append("未达到自动优先核验条件")
    # The priority bucket means "review first", not employer-confirmed still-open.
    ready=(not reasons and row.get("status")=="active")
    row["review_bucket"]="priority" if ready else "lead"
    row["review_reasons"]=[] if ready else list(dict.fromkeys(reasons or ["等待人工核验"]))
    # Keep lifecycle status separate from the review bucket. A fresh successful
    # link check on a later run may promote an active candidate from lead to priority.
    return ready
