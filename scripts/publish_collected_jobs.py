import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
J=ROOT/"data/jobs.json"; I=ROOT/"data/jobs.inbox.json"
existing=json.loads(J.read_text(encoding="utf-8")); incoming=json.loads(I.read_text(encoding="utf-8"))
def key(x):
    return (x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("city","").strip().lower(),x.get("cohort","").strip().lower())
idx={key(x):x for x in existing}; by_url={x.get("source_url"):x for x in existing if x.get("source_url")}; by_role={(x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("cohort","").strip().lower()):x for x in existing}; added=updated=0
for x in incoming:
    k=key(x)
    if not all(k): continue
    old=idx.get(k) or by_url.get(x.get("source_url"))
    if not old and x.get("city")=="全国": old=by_role.get((x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("cohort","").strip().lower()))
    if old:
        old["last_verified"]=x.get("last_verified") or old.get("last_verified","")
        old["collector"]=x.get("collector",old.get("collector",""))
        if x.get("verification_score",0)>old.get("verification_score",0):
            for f in ("status","granularity","verification_score","source_url","source","program","confirmed_by"): old[f]=x.get(f,old.get(f))
        updated+=1
    else:
        idx[k]=x; added+=1
        if x.get("source_url"): by_url[x.get("source_url")]=x
        by_role[(x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("cohort","").strip().lower())]=x
J.write_text(json.dumps(list(idx.values()),ensure_ascii=False,indent=2),encoding="utf-8")
print(f"incoming={len(incoming)} added={added} updated={updated} total={len(idx)}")
