"""Merge collected public job records without promoting article/announcement rows."""
import json
import re
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from collect_public_sources import clean_job_title, is_direct_detail_url, public_lead_dedupe_key, should_purge_qzw_noise_record
from quality_rules import assess, should_publish_record

ROOT=Path(__file__).resolve().parents[1]
J=ROOT/"data/jobs.json"
I=ROOT/"data/jobs.inbox.json"
MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=date.today().isoformat()

def title_source_id(row):
    source=str(row.get("source",""))
    if source=="牛客公开岗位": return "nowcoder"
    if "应届生求职网" in source: return "yingjiesheng"
    if "秋招网" in source: return "qiuzhaowang"
    if "BOSS直聘" in source: return "boss"
    if "国聘" in source: return "iguopin"
    if "前程无忧" in source or "51job" in source.lower(): return "51job"
    return ""

TRACKING={"utm_source","utm_medium","utm_campaign","utm_term","utm_content","spm","from","referrer","trackingid","property","requestid","policytype","policyid","pagecode","pagesource","isinitiative","issuggest","jobrank","advid"}

def norm_url(url):
    if not url: return ""
    p=urlsplit(str(url).strip())
    query=sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in TRACKING)
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/"),"",urlencode(query,doseq=True)))

def role_key(x):
    if x.get("lead_evidence")=="visible_public_job_card":
        return ("public-list-lead",)+public_lead_dedupe_key(x)
    return ("role",
            str(x.get("company","")).strip().lower(),
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
    row["title"]=clean_job_title(row.get("title",""),title_source_id(row))
    if not row.get("company") or not row.get("title") or not row.get("source_url"): continue
    row.setdefault("last_collected",TODAY)
    row.setdefault("last_verified","")
    row.setdefault("last_seen",row.get("last_collected") or TODAY)
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
                      "last_verified","last_verified_at","verification_status","verification_http_status","verification_note",
                      "lead_evidence","source_page_title","source_card_text","public_card_lead")
        # When a direct detail page is revisited, refresh actual visible fields.
        if row.get("source_url") and (row.get("source")=="牛客公开岗位" or is_direct_detail_url(row.get("source_url","")) or row.get("lead_evidence")=="visible_public_job_card"):
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

# Clean old records from the polluted V3 title pattern, rejected list-only leads, and legacy QZW title noise.
removed_public_list_leads=0
removed_qzw_title_noise=0
for key,row in list(idx.items()):
    row["title"]=clean_job_title(row.get("title",""),title_source_id(row))
    if row.get("lead_evidence")=="visible_public_job_card" and not should_publish_record(row):
        del idx[key]
        removed_public_list_leads+=1
        continue
    if should_purge_qzw_noise_record(row):
        del idx[key]
        removed_qzw_title_noise+=1
        continue
    if not row.get("last_seen"): row["last_seen"]=row.get("last_collected") or row.get("last_verified","") or ""
    if not row.get("last_collected"): row["last_collected"]=row.get("last_seen","")
    if row.get("collector")=="public-html-v3" and not row.get("last_verified_at"):
        row["last_verified"]=""
        row["verification_status"]="stale"
    assess(row)

def merge_same_id(rows):
    """Collapse duplicates caused by one detail URL being parsed with conflicting list-page metadata."""
    merged={}
    richer_fields=("salary","degree","deadline","publish_date","description","responsibilities",
                   "requirements","benefits","company_info","address","work_time","experience",
                   "job_type","headcount","program")
    placeholders={"","待核验","未公开","未提取","待确认","全国","暂无","未知"}
    def richness(row):
        count=sum(1 for field in richer_fields if str(row.get(field,"") or "").strip() not in placeholders)
        detail_len=sum(len(str(row.get(field,"") or "")) for field in ("description","responsibilities","requirements"))
        return (count, detail_len, 1 if row.get("verification_status")=="link_alive" else 0)
    for row in rows:
        rid=str(row.get("id",""))
        if not rid:
            merged["__missing__"+str(len(merged))]=row
            continue
        if rid not in merged:
            merged[rid]=row
            continue
        old=merged[rid]
        winner,other=(row,old) if richness(row)>richness(old) else (old,row)
        for field in richer_fields:
            a=str(winner.get(field,"") or "").strip()
            b=str(other.get(field,"") or "").strip()
            if a in placeholders and b not in placeholders:
                winner[field]=other[field]
            elif field in {"description","responsibilities","requirements","benefits","company_info"} and len(b)>len(a) and b not in placeholders:
                winner[field]=other[field]
        for field in ("city","keywords","confirmed_by"):
            vals=[]
            for source_row in (winner,other):
                raw=source_row.get(field,"")
                vals.extend(raw if isinstance(raw,list) else re.split(r"[、,，;；]",str(raw or "")))
            vals=list(dict.fromkeys(v.strip() for v in vals if v and v.strip() not in placeholders))
            winner[field]="、".join(vals) if field=="city" and vals else ("全国" if field=="city" else vals)
        # Conflicting cohort claims cannot be combined into stronger evidence.
        if winner.get("cohort")!=other.get("cohort") and winner.get("cohort_evidence") not in {"page_text","visible_announcement_title","job_detail_title","job_detail_text"}:
            winner["cohort"]=""; winner["cohort_evidence"]=""; winner["cohort_confirmed"]=False
        for field in ("last_collected","last_seen"):
            winner[field]=max(str(winner.get(field,"") or ""),str(other.get(field,"") or ""))
        terminal={"closed","expired","withdrawn","cancelled"}
        if winner.get("status") not in terminal and other.get("status") in terminal:
            winner["status"]=other["status"]
        merged[rid]=winner
    return list(merged.values())

published_before_filter=merge_same_id(list(idx.values()))
published=[row for row in published_before_filter if should_publish_record(row)]
quality_filter_removed=len(published_before_filter)-len(published)
for row in published:
    assess(row)
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
    manifest["quality_gate_version"]="2.3"
    manifest["source_quality_filters"]={
        "aggregator_non_detail_or_non_role_records_removed":quality_filter_removed,
        "policy":"聚合来源的独立详情岗位必须有明确岗位名与岗位级详情链接；仅国聘/前程无忧公开职位列表中带真实企业名、具体岗位名和应届/校招证据的无详情卡片，才可作为低置信待核验线索。搜索入口、企业介绍、HR话术、行业目录不得作为岗位，公告与官方入口单独保留。BOSS公开卡片因详情访问受限单独统计，不能冒充已核实详情岗位。",
        "collector_title_rule":"is_specific_role_title",
        "sources":["秋招网","应届生求职网","BOSS直聘","牛客","国聘","前程无忧51job"],
        "list_only_lead_sources":["国聘","前程无忧51job"],
        "public_card_only_sources":["BOSS直聘"],
        "stale_cycle_url_pruning":["前程无忧校园招聘微站明确标记非2027届的页面不进入轮转池"]
    }
    MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
except Exception as exc:
    raise RuntimeError(f"Unable to update collection manifest: {exc}") from exc
print(f"incoming={len(incoming)} added={added} updated={updated} removed_public_list_leads={removed_public_list_leads} removed_qzw_title_noise={removed_qzw_title_noise} total={len(published)} priority={counts['priority']} leads={counts['lead']}")
