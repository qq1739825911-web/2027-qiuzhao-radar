"""Recalculate review buckets after URL verification and deduplication."""
import json
from pathlib import Path
from datetime import datetime, timezone

from quality_rules import assess, compact_json

ROOT=Path(__file__).resolve().parents[1]
JOBS=ROOT/"data/jobs.json"
MANIFEST=ROOT/"data/collection-manifest.json"
STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()

jobs=json.loads(JOBS.read_text(encoding="utf-8"))
priority=lead=0
for row in jobs:
    if assess(row): priority+=1
    else: lead+=1

JOBS.write_text(compact_json(jobs),encoding="utf-8")
try:
    manifest=json.loads(MANIFEST.read_text(encoding="utf-8"))
except Exception:
    manifest={}
manifest["quality_gate_version"]="2.1"
manifest["quality_checked_at"]=STAMP
manifest["published_total_jobs"]=len(jobs)
manifest["published_priority_candidates"]=priority
manifest["published_pending_review"]=lead
manifest["review_bucket_note"]="可优先核验仅表示岗位详情链接近期可访问、职位名和2027届证据满足规则；不代表雇主确认仍在招聘。"
MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"FINAL QUALITY: total={len(jobs)} priority={priority} leads={lead} checked_at={STAMP}")
