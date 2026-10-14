"""Recalculate review buckets after URL verification and deduplication."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone

from quality_rules import assess, compact_json

ROOT=Path(__file__).resolve().parents[1]
JOBS=ROOT/"data/jobs.json"
MANIFEST=ROOT/"data/collection-manifest.json"
STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()

jobs=json.loads(JOBS.read_text(encoding="utf-8"))
priority=lead=0
text_fields=("description","responsibilities","requirements","benefits","company_info","address","work_time")
known_json_key=re.compile(r"\b(?:jobCity|careerJobId|deliverBegin|deliverEnd|refreshBegin|latestProcessTime|graduationYear|salaryType|salaryMin|salaryMax|jobKeys|companyId|companyName|positionId|positionName|recruitJobName|jobName|jobOffer|eduLevel)\s*:",re.I)
legacy_css=re.compile(r"\.ie(?:11|undefined)\b",re.I)
tail_noise=re.compile(r"(?:联系方式|联系人|联系电话|联系邮箱|简历接收邮箱|简历投递|投递方式|投递邮箱|查看其他岗位|查看更多职位|投递说明|招聘流程|公司介绍|公司简介|上一条|下一条|上一篇|下一篇|返回顶部|查看原发布网址)",re.I)
company_noise=re.compile(r"(?:职位类别|职位分类|岗位类别|岗位分类|专业标签|招聘人数|工作地点|工作城市|工作地区|学历要求|职位描述|岗位职责|岗位要求|任职要求|职位类型|投递说明|联系方式)\s*[:：]?",re.I)
title_prefix=re.compile(r"^\s*(?:20\d{2}|2\d)\s*(?:届\s*)?(?:校园招聘|校招|招聘)")
cleanup={
    "rows":0,"fields_checked":0,"fields_changed":0,"records_changed":0,
    "json_markers_before":0,"json_markers_remaining":0,
    "css_markers_before":0,"css_markers_remaining":0,
    "tail_noise_before":0,"tail_noise_remaining":0,"samples":[],
    "company_fields_checked":0,"company_fields_changed":0,"company_metadata_before":0,"company_metadata_remaining":0,
    "title_prefix_before":0,"title_prefix_remaining":0
}
for row in jobs:
    is_yjs="应届生求职网" in str(row.get("source",""))
    before={field:str(row.get(field,"") or "") for field in text_fields} if is_yjs else {}
    company_before=str(row.get("company","") or "") if is_yjs else ""
    title_before=str(row.get("title","") or "") if is_yjs else ""
    if is_yjs:
        cleanup["rows"]+=1
        cleanup["company_fields_checked"]+=1
        if company_noise.search(company_before): cleanup["company_metadata_before"]+=1
        if title_prefix.search(title_before): cleanup["title_prefix_before"]+=1
        for value in before.values():
            cleanup["fields_checked"]+=1
            if known_json_key.search(value): cleanup["json_markers_before"]+=1
            if legacy_css.search(value): cleanup["css_markers_before"]+=1
            if tail_noise.search(value): cleanup["tail_noise_before"]+=1
    ok=assess(row)
    if ok: priority+=1
    else: lead+=1
    if is_yjs:
        company_after=str(row.get("company","") or "")
        title_after=str(row.get("title","") or "")
        if company_after!=company_before: cleanup["company_fields_changed"]+=1
        if company_noise.search(company_after): cleanup["company_metadata_remaining"]+=1
        if title_prefix.search(title_after): cleanup["title_prefix_remaining"]+=1
        changed_fields=[field for field,value in before.items() if str(row.get(field,"") or "")!=value]
        cleanup["fields_changed"]+=len(changed_fields)
        if changed_fields:
            cleanup["records_changed"]+=1
            if len(cleanup["samples"])<15:
                cleanup["samples"].append({
                    "id":row.get("id",""),"title":str(row.get("title",""))[:100],
                    "fields_changed":changed_fields,
                    "before_lengths":{field:len(before[field]) for field in changed_fields},
                    "after_lengths":{field:len(str(row.get(field,"") or "")) for field in changed_fields}
                })
        for field in text_fields:
            value=str(row.get(field,"") or "")
            if known_json_key.search(value): cleanup["json_markers_remaining"]+=1
            if legacy_css.search(value): cleanup["css_markers_remaining"]+=1
            if tail_noise.search(value): cleanup["tail_noise_remaining"]+=1

JOBS.write_text(compact_json(jobs),encoding="utf-8")
try:
    manifest=json.loads(MANIFEST.read_text(encoding="utf-8"))
except Exception:
    manifest={}
manifest["quality_gate_version"]="2.3"
manifest["yingjiesheng_text_cleanup"]=cleanup
manifest["quality_checked_at"]=STAMP
manifest["published_total_jobs"]=len(jobs)
manifest["published_priority_candidates"]=priority
manifest["published_pending_review"]=lead
manifest["review_bucket_note"]="可优先核验仅表示岗位详情链接近期可访问、职位名和2027届证据满足规则；不代表雇主确认仍在招聘。"
MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"FINAL QUALITY: total={len(jobs)} priority={priority} leads={lead} checked_at={STAMP}")
