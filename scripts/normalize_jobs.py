import json, hashlib
from pathlib import Path
from datetime import date

ROOT=Path(__file__).resolve().parents[1]
INBOX=ROOT/"data/jobs.inbox.json"
OUT=ROOT/"data/jobs.normalized.json"

def stable_id(row):
    raw="|".join([
        str(row.get("company","")).strip().lower(),
        str(row.get("title","")).strip().lower(),
        str(row.get("city","")).strip().lower(),
        str(row.get("source_url","")).strip().lower(),
    ])
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12],16)

def normalize(row):
    x=dict(row)
    x["company"]=str(x.get("company","")).strip()
    x["title"]=str(x.get("title","")).strip()
    x["category"]=x.get("category") or x.get("industry") or "其他"
    x["city"]=x.get("city") or "全国"
    x["enterprise_type"]=x.get("enterprise_type") or "未分类"
    x["degree"]=x.get("degree") or "不限"
    x["source"]=x.get("source") or "待核验"
    x["source_url"]=x.get("source_url") or ""
    x["status"]=x.get("status") or "active"
    x["keywords"]=x.get("keywords") if isinstance(x.get("keywords"),list) else str(x.get("keywords","")).split()
    x["id"]=x.get("id") or stable_id(x)
    return x

rows=json.loads(INBOX.read_text(encoding="utf-8"))
normalized=[normalize(x) for x in rows if x.get("company") and x.get("title")]
seen=set(); result=[]
for x in normalized:
    key=(x["company"].lower(),x["title"].lower(),x["city"].lower(),x["source_url"].lower())
    if key in seen: continue
    seen.add(key); result.append(x)
OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"normalized={len(result)}")
