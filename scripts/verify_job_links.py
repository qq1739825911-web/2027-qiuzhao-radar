"""Conservative scheduled link verifier for direct job-detail URLs.
Only public HTTP(S) pages are checked. No login, private APIs, CAPTCHA or anti-bot bypass.
A link is marked closed only after two consecutive 404/410 or explicit closed markers.
"""
import json, re, time
from pathlib import Path
from datetime import datetime, timezone, date
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
JOBS=ROOT/"data/jobs.json"; STATE=ROOT/"data/verification-state.json"; MANIFEST=ROOT/"data/verification-manifest.json"
TODAY=str(date.today()); STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
HEADERS={"User-Agent":"2027-Qiuzhao-Radar-PublicVerifier/1.0"}
DIRECT=re.compile(r"/(?:jobs/detail/|jobs/job/|campus/position/[^/]+/detail|position/[^/]+/detail|job/detail/|job/position/)",re.I)
CLOSED_MARKERS=("职位已下线","岗位已下线","职位不存在","岗位不存在","招聘已结束","投递已结束","职位已关闭","岗位已关闭","该职位已失效")
def fetch(url):
    req=Request(url,headers=HEADERS)
    try:
        with urlopen(req,timeout=8) as r:
            body=r.read(250000).decode(r.headers.get_content_charset() or "utf-8",errors="ignore")
            return r.status,body
    except HTTPError as e: return e.code,""
    except (URLError,TimeoutError,ValueError): return 0,""
def main():
    jobs=json.loads(JOBS.read_text(encoding="utf-8"))
    try: state=json.loads(STATE.read_text(encoding="utf-8"))
    except Exception: state={"cursor":0,"failures":{}}
    failures=state.setdefault("failures",{})
    eligible=[j for j in jobs if not j.get("demo") and j.get("source_url","").startswith("https://") and DIRECT.search(urlparse(j.get("source_url","")).path)]
    eligible.sort(key=lambda j:(j.get("last_verified",""),j.get("company",""),j.get("title","")))
    batch_size=60
    if eligible:
        start=int(state.get("cursor",0))%len(eligible); batch=(eligible+eligible)[start:start+min(batch_size,len(eligible))]
    else: start=0; batch=[]
    alive=missing=unknown=closed=0; checked=[]
    def check(job):
        status,body=fetch(job.get("source_url",""))
        if status in (404,410) or (status==200 and any(m in body for m in CLOSED_MARKERS)): return "missing",status
        if 200<=status<400: return "alive",status
        return "unknown",status
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(check,j):j for j in batch}
        for fut in as_completed(futures):
            job=futures[fut]
            try: result,status=fut.result()
            except Exception: result,status="unknown",0
            checked.append({"id":job.get("id"),"result":result,"http_status":status})
            jid=str(job.get("id",""))
            if result=="alive":
                alive+=1; failures[jid]=0; job["verification_status"]="link_alive"; job["last_verified"]=TODAY; job["last_verified_at"]=STAMP; job["verification_http_status"]=status
                job["verification_note"]="公开详情链接可访问；不代表招聘条件已由雇主再次确认"
            elif result=="missing":
                missing+=1; failures[jid]=int(failures.get(jid,0))+1; job["verification_status"]="missing_recheck"; job["verification_http_status"]=status
                job["verification_note"]=f"详情链接异常，连续核验 {failures[jid]} 次"
                if failures[jid]>=2:
                    job["status"]="closed"; job["verification_status"]="closed"; job["verification_note"]="连续两次核验确认链接失效/岗位下线"; closed+=1
            else:
                unknown+=1; job["verification_status"]="unknown"; job["verification_http_status"]=status; job["verification_note"]="访问受限或暂时失败，未据此关闭岗位"
            time.sleep(0.05)
    state.update({"cursor":(start+len(batch))%max(1,len(eligible)),"last_run_at":STAMP,"last_batch_size":len(batch),"eligible_count":len(eligible)})
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    MANIFEST.write_text(json.dumps({"last_run_at":STAMP,"checked":len(batch),"eligible_direct_links":len(eligible),"link_alive":alive,"missing_needs_recheck":missing,"unknown":unknown,"closed_after_two_checks":closed,"batch_size":batch_size,"note":"链接可访问仅代表页面可打开，不等于岗位仍在招或已满足2027届条件。","checked_items":checked},ensure_ascii=False,indent=2),encoding="utf-8")
    JOBS.write_text(json.dumps(jobs,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"LINK VERIFICATION: checked={len(batch)} eligible={len(eligible)} alive={alive} missing_recheck={missing} unknown={unknown} closed_after_two_checks={closed}")
if __name__=="__main__": main()
