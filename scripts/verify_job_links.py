"""Conservative scheduled link verifier for public job-detail pages.
Only public HTTP(S) pages are checked. No login, private APIs, CAPTCHA or anti-bot bypass.
A link is marked closed only after two 404/410 or explicit closed-marker checks separated by >=5h45m.
"""
import json, re, time
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
JOBS=ROOT/"data/jobs.json"; STATE=ROOT/"data/verification-state.json"; MANIFEST=ROOT/"data/verification-manifest.json"
TODAY=str(date.today()); STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
HEADERS={"User-Agent":"2027-Qiuzhao-Radar-PublicVerifier/1.1"}
# Covers Nowcoder details, Xiaomi/Baidu detail pages, and Qiuzhaowang /jobs/<slug> pages.
DIRECT=re.compile(r"/(?:jobs/detail/\d+|jobs/hr/\d+|jobs/(?!latest(?:/|$)|list(?:/|$))[^/?#]+|jobdetail/\d+|job-\d{3}-\d{3}-\d+\.html|positions/[^/?#]+|campus/position/[^/]+/detail|position/[^/]+/detail|job/detail/|job/position/|jobdesc\.html|web/position/detail)",re.I)

def is_direct_detail(url):
    p=urlparse(url); path=p.path.lower(); query=p.query.lower()
    if not DIRECT.search(path): return False
    if "/jobs/hr/" in path and not re.search(r"(?:^|&)jobid=\d+",query): return False
    if path.endswith("/jobdesc.html") and not re.search(r"(?:^|&)postid=[^&]+",query): return False
    if path.endswith("/web/position/detail") and not re.search(r"(?:^|&)jobunionid=\d+",query): return False
    return True
CLOSED_MARKERS=("职位已下线","岗位已下线","职位不存在","岗位不存在","招聘已结束","投递已结束","职位已关闭","岗位已关闭","该职位已失效")
GAP=timedelta(hours=5,minutes=45)
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
    previous=state.get("last_run_at")
    failures=state.setdefault("failures",{}); last_missing=state.setdefault("last_missing_at",{})
    now_dt=datetime.now(timezone.utc); repaired=0
    for job in jobs:
        jid=str(job.get("id",""))
        if job.get("status")=="closed" and "连续两次核验确认链接失效" in job.get("verification_note",""):
            prior=last_missing.get(jid) or previous
            try: elapsed=now_dt-datetime.fromisoformat(prior.replace("Z","+00:00"))
            except Exception: elapsed=timedelta(0)
            if elapsed<GAP:
                job["status"]="pending_review"; job["verification_status"]="missing_recheck"
                job["verification_note"]="上个版本两次异常间隔不足6小时，恢复待复查；需要下一轮定时核验确认"
                failures[jid]=1; last_missing[jid]=prior or STAMP; repaired+=1
    if previous:
        try:
            prev_dt=datetime.fromisoformat(previous.replace("Z","+00:00"))
            if now_dt-prev_dt<GAP:
                if repaired:
                    JOBS.write_text(json.dumps(jobs,ensure_ascii=False,indent=2),encoding="utf-8")
                    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
                    try:
                        old=json.loads(MANIFEST.read_text(encoding="utf-8"))
                        old["closed_after_two_checks"]=max(0,int(old.get("closed_after_two_checks",0))-repaired)
                        old["corrected_premature_closures"]=repaired
                        old["note"]="已修复间隔不足6小时的误关闭；等待下一轮定时核验确认。"
                        MANIFEST.write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding="utf-8")
                    except Exception: pass
                print(f"LINK VERIFICATION SKIPPED: previous batch was {previous}; next batch is scheduled six-hourly.")
                return
        except Exception: pass

    # Group multiple role rows that share one listing/detail URL. Check each unique URL once,
    # then propagate the result to every role tied to that URL.
    by_url={}
    for job in jobs:
        url=job.get("source_url","")
        if job.get("demo") or not url.startswith("https://") or not is_direct_detail(url): continue
        by_url.setdefault(url,[]).append(job)
    eligible_urls=sorted(by_url,key=lambda u:(min((j.get("last_verified","") for j in by_url[u]),default=""),u))
    batch_size=120
    if eligible_urls:
        start=int(state.get("cursor",0))%len(eligible_urls)
        batch_urls=(eligible_urls+eligible_urls)[start:start+min(batch_size,len(eligible_urls))]
    else: start=0; batch_urls=[]
    alive=missing=unknown=closed=records_touched=0; checked=[]
    def check_url(url):
        status,body=fetch(url)
        if status in (404,410) or (status==200 and any(m in body for m in CLOSED_MARKERS)): return "missing",status
        if 200<=status<400: return "alive",status
        return "unknown",status
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures={pool.submit(check_url,url):url for url in batch_urls}
        for fut in as_completed(futures):
            url=futures[fut]; related=by_url[url]
            try: result,status=fut.result()
            except Exception: result,status="unknown",0
            checked.append({"url":url[:240],"result":result,"http_status":status,"records":len(related)})
            records_touched+=len(related)
            for job in related:
                jid=str(job.get("id",""))
                if result=="alive":
                    job["verification_status"]="link_alive"; job["last_verified"]=TODAY; job["last_verified_at"]=STAMP; job["verification_http_status"]=status
                    job["verification_note"]="公开详情链接可访问；不代表招聘条件已由雇主再次确认"
                    failures[jid]=0; last_missing.pop(jid,None)
                elif result=="missing":
                    missing+=1; job["verification_http_status"]=status
                    prior=last_missing.get(jid); second_confirmation=False
                    if not prior:
                        failures[jid]=max(1,int(failures.get(jid,0))); last_missing[jid]=STAMP
                    else:
                        try: elapsed=now_dt-datetime.fromisoformat(prior.replace("Z","+00:00"))
                        except Exception: elapsed=timedelta(0)
                        if elapsed>=GAP:
                            failures[jid]=int(failures.get(jid,1))+1; last_missing[jid]=STAMP; second_confirmation=True
                    job["verification_status"]="missing_recheck"
                    job["verification_note"]=f"详情链接异常，已确认 {failures.get(jid,1)} 次；需至少间隔6小时再次确认"
                    if failures.get(jid,1)>=2 and second_confirmation:
                        job["status"]="closed"; job["verification_status"]="closed"
                        job["verification_note"]="间隔至少6小时的两次核验均确认链接失效/岗位下线"; closed+=1
                else:
                    job["verification_status"]="unknown"; job["verification_http_status"]=status
                    job["verification_note"]="访问受限或暂时失败，未据此关闭岗位"
            if result=="alive": alive+=1
            elif result=="unknown": unknown+=1
            time.sleep(0.03)

    cutoff=(date.today()-timedelta(days=7)).isoformat()
    for job in jobs:
        if job.get("demo") or job.get("status")=="closed" or job.get("verification_status")=="missing_recheck": continue
        last=job.get("last_verified","")
        if not last or last<cutoff:
            job["verification_status"]="stale"
            job["verification_note"]="最近一次核验超过7天或缺少核验日期；需重新核对，未据此关闭岗位"
        elif job.get("verification_status")=="stale":
            job["verification_status"]="not_checked"; job.pop("verification_note",None)
    state.update({"cursor":(start+len(batch_urls))%max(1,len(eligible_urls)),"last_run_at":STAMP,"last_batch_size":len(batch_urls),"eligible_count":len(eligible_urls)})
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    MANIFEST.write_text(json.dumps({"last_run_at":STAMP,"checked":len(batch_urls),"records_touched":records_touched,"eligible_direct_links":len(eligible_urls),"link_alive":alive,"missing_needs_recheck":missing,"unknown":unknown,"closed_after_two_checks":closed,"batch_size":batch_size,"note":"链接可访问仅代表页面可打开，不等于岗位仍在招或已满足2027届条件；同一链接下的多个岗位共用一次检查结果。","checked_items":checked},ensure_ascii=False,indent=2),encoding="utf-8")
    JOBS.write_text(json.dumps(jobs,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"LINK VERIFICATION: checked_unique_urls={len(batch_urls)} eligible_unique_urls={len(eligible_urls)} records_touched={records_touched} alive={alive} missing_records={missing} unknown_urls={unknown} closed_after_two_checks={closed}")
if __name__=="__main__": main()
