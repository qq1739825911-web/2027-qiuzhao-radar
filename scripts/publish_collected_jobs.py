"""Merge collected public job records without promoting article/announcement rows."""
import json
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from collect_public_sources import clean_job_title, is_direct_detail_url
from quality_rules import assess

ROOT=Path(__file__).resolve().parents[1]
J=ROOT/"data/jobs.json"
I=ROOT/"data/jobs.inbox.json"
MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=date.today().isoformat()
TRACKING={"utm_source","utm_medium","utm_campaign","utm_term","utm_content","spm","from","referrer","trackingid","property","requestid","policytype","policyid","pagecode","pagesource","isinitiative","issuggest","jobrank","advid"}

def norm_url(url):
    if not url: return ""
    p=urlsplit(str(url).strip())
    query=sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in TRACKING)
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/"),"",urlencode(query,doseq=True)))

def role_key(x):
    return (str(x.get("company","")).strip().lower(),
            str(x.get("title","")).strip().lower(),
            str(x.get("city","")).strip().lower(),
            str(x.get("cohort","")).strip().lower(),
            norm_url(x.get("source_url","")))

existing=json.loads(J.read_text(encoding="utf-8"))
incoming=json.loads(I.read_text(encoding="utf-8"))
idx={role_key(row):row for row in existing}
by_url={}
by_nowcoder_url={}
by_role={}
for row in idx.values():
    u=norm_url(row.get("source_url",""))
    if u and row.get("source")=="牛客公开岗位": by_nowcoder_url[u]=row
    if u: by_url.setdefault(u,[]).append(row)
    by_role[(str(row.get("company","")).strip().lower(),str(row.get("title","")).strip().lower(),str(row.get("cohort","")).strip().lower())]=row
added=updated=0

for row in incoming:
    row=dict(row)
    row["title"]=clean_job_title(row.get("title",""),"nowcoder" if row.get("source")=="牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in str(row.get("source","")) else ""))
    if not row.get("company") or not row.get("title") or not row.get("source_url"): continue
    row.setdefault("last_collected",TODAY)
    row.setdefault("last_verified","")
    k=role_key(row)
    old=idx.get(k)
    url_key=norm_url(row.get("source_url",""))
    # A direct aggregator detail URL represents one role; a shared announcement URL does not.
    if not old and row.get("source")=="牛客公开岗位":
        old=by_nowcoder_url.get(url_key)
    if not old and row.get("granularity")=="job":
        old=by_role.get((str(row["company"]).strip().lower(),str(row["title"]).strip().lower(),str(row.get("cohort","")).strip().lower()))
    if old:
        # Collection time must never overwrite the timestamp of the last actual URL check.
        prior_collector=old.get("collector","")
        old["last_collected"]=row.get("last_collected") or TODAY
        old["last_seen"]=TODAY
        old["collector"]=row.get("collector",old.get("collector",""))
        merge_fields=("company","title","category","city","enterprise_type","degree","salary","deadline","publish_date",
                      "source","source_url","cohort","cohort_evidence","cohort_confirmed","program","granularity","status",
                      "verification_score","confirmed_by","description","responsibilities","requirements","benefits",
                      "company_info","address","work_time","experience","job_type","headcount","keywords",
                      "last_verified","last_verified_at","verification_status","verification_http_status","verification_note")
        # When a direct detail page is revisited, refresh actual visible fields.
        if row.get("source_url") and (row.get("source")=="牛客公开岗位" or is_direct_detail_url(row.get("source_url",""))):
            for field in merge_fields:
                value=row.get(field)
                if value not in (None,"",[],{}): old[field]=value
        else:
            for field in ("last_collected","last_seen","collector"):
                old[field]=row[field]
        if row.get("verification_score",0)>old.get("verification_score",0):
            old["verification_score"]=row["verification_score"]
        # For legacy V3 rows, dates set by collection were not genuine verification timestamps.
        if prior_collector=="public-html-v3" and not old.get("last_verified_at") and not row.get("last_verified_at"):
            old["last_verified"]=""
            old["verification_status"]="stale"
        updated+=1
    else:
        row["last_seen"]=TODAY
        idx[k]=row
        added+=1
        if row.get("source")=="牛客公开岗位": by_nowcoder_url[url_key]=row
        if url_key: by_url.setdefault(url_key,[]).append(row)
        by_role[(str(row["company"]).strip().lower(),str(row["title"]).strip().lower(),str(row.get("cohort","")).strip().lower())]=row

# Clean old records from the polluted V3 title pattern and prevent the old implicit cohort default being trusted.
for row in idx.values():
    row["title"]=clean_job_title(row.get("title",""),"nowcoder" if row.get("source")=="牛客公开岗位" else ("yingjiesheng" if "应届生求职网" in str(row.get("source","")) else ""))
    if not row.get("last_seen"): row["last_seen"]=row.get("last_collected") or row.get("last_verified","") or ""
    if not row.get("last_collected"): row["last_collected"]=row.get("last_seen","")
    if row.get("collector")=="public-html-v3" and not row.get("last_verified_at"):
        row["last_verified"]=""
        row["verification_status"]="stale"
    assess(row)

published=list(idx.values())
J.write_text(json.dumps(published,ensure_ascii=False,indent=2),encoding="utf-8")
try:
    manifest=json.loads(MANIFEST.read_text(encoding="utf-8"))
    counts={"priority":0,"lead":0}
    demoted=0
    for row in published:
        counts[row.get("review_bucket","lead")]=counts.get(row.get("review_bucket","lead"),0)+1
    manifest["published_total_jobs"]=len(published)
    manifest["published_priority_candidates"]=counts["priority"]
    manifest["published_pending_review"]=counts["lead"]
    manifest["records_added_this_run"]=added
    manifest["records_updated_this_run"]=updated
    manifest["quality_gate_version"]="2.0"
    MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
except Exception as exc:
    raise RuntimeError(f"Unable to update collection manifest: {exc}") from exc
print(f"incoming={len(incoming)} added={added} updated={updated} total={len(published)} priority={counts['priority']} leads={counts['lead']}")
