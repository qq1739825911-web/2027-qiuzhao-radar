import json
from datetime import date

REQUIRED={"id","company","title","category","city","enterprise_type","source","source_url","last_verified","status"}

with open("data/jobs.json","r",encoding="utf-8") as f:
    jobs=json.load(f)

errors=[]
seen=set()
for job in jobs:
    missing=REQUIRED-set(job)
    if missing: errors.append(f"id={job.get('id')}: missing {sorted(missing)}")
    if job.get("id") in seen: errors.append(f"duplicate id={job.get('id')}")
    seen.add(job.get("id"))
    for field in ("publish_date","deadline","last_verified"):
        if job.get(field):
            try: date.fromisoformat(job[field])
            except ValueError: errors.append(f"id={job.get('id')}: invalid {field}")

if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print(f"OK: validated {len(jobs)} jobs")
