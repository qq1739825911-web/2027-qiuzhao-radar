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
    old=idx.get(k)
    # A listing/article URL can contain many distinct jobs, so URL alone is not a unique job key.
    # Use URL-based correction only for the one-role-per-detail-page aggregator (Nowcoder).
    if not old and x.get("source")=="牛客公开岗位":
        old=by_url.get(x.get("source_url"))
    if not old and x.get("city")=="全国":
        old=by_role.get((x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("cohort","").strip().lower()))
    if old:
        old["last_verified"]=x.get("last_verified") or old.get("last_verified","")
        old["collector"]=x.get("collector",old.get("collector",""))
        # When an aggregator seed was initially stored as the platform name, correct it
        # from the public detail page while retaining the direct source URL.
        if x.get("source")=="牛客公开岗位":
            for f in ("company","title","category","city","degree","source","source_url","cohort","program","granularity","verification_score","confirmed_by"):
                if x.get(f) not in (None,""): old[f]=x[f]
        elif x.get("verification_score",0)>old.get("verification_score",0):
            for f in ("status","granularity","verification_score","source_url","source","program","confirmed_by"): old[f]=x.get(f,old.get(f))
        updated+=1
    else:
        idx[k]=x; added+=1
        if x.get("source_url"): by_url[x.get("source_url")]=x
        by_role[(x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("cohort","").strip().lower())]=x
# Downgrade broad headings / qualification fragments previously emitted from this public article.
for row in idx.values():
    if (row.get("source_url","").startswith("https://m.yingjiesheng.com/xuanjianghui/xjh_6268864")
        and row.get("source")=="应届生求职网公开岗位"):
        title=row.get("title","").strip()
        if (len(title)>45 or title.endswith(("类","方向","相关专业","等相关专业"))
            or title.startswith(("具有","负责","熟悉","掌握","参与","岗位","要求"))):
            row["status"]="pending_review"
            row["granularity"]="review"
            row["verification_score"]=min(row.get("verification_score",5),5)
J.write_text(json.dumps(list(idx.values()),ensure_ascii=False,indent=2),encoding="utf-8")
print(f"incoming={len(incoming)} added={added} updated={updated} total={len(idx)}")
