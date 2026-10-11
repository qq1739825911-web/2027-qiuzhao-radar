"""V5.5 public recruitment collector.

Rules:
- public official pages only; no login/CAPTCHA/private API/anti-bot bypass.
- prefer concrete job-level listings.
- program/announcement pages are never promoted to active jobs.
- extract visible links, HTML tables/lists, JSON-LD and common embedded JSON.
- score records and only promote high-confidence concrete roles.
"""
import time
import json,re,hashlib,atexit,subprocess,threading
from pathlib import Path
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse,urlunparse,parse_qsl,urlencode,quote
from urllib.request import Request,urlopen
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/"data/company_sources.json"; INBOX=ROOT/"data/jobs.inbox.json"; MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=str(date.today())
STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8","Accept-Language":"zh-CN,zh;q=0.9,en;q=0.8","Cache-Control":"no-cache"}

JOB_WORDS=("工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理")
CONCRETE_WORDS=("AIGC","AI产品","AI应用","视频","影视","创意制作","制作","创作","动画","短视频","剪辑","编导","文案","游戏","直播","摄影","工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","研究岗","行业研究","分析师","分析岗","管培生","管理培训生","培训生","咨询","教师","医生","护士","质量","项目经理","策划","内容运营","新媒体","客服","行政","人事","风控","风险","信贷","会计","策略","助理","客户经理","技术支持","商业分析","投资","证券","保险","精算","编辑","翻译","业务专员","运营岗","产品运营","品牌","公关","电商","市场拓展","Sales","Marketing","Analyst","Engineer","Intern","Associate","Manager","Specialist","Consultant","Accountant","Finance","Risk","Audit","Legal","Research","Strategy","Operations","Supply Chain","Procurement","Quality","Manufacturing","Mechanical","Electrical","Clinical","Medical","Education","Teacher","Editor","Content","Copywriter","Designer","Design","Product","Business","HR","Data Science","Data Analyst")
PROGRAM_WORDS=("招聘公告","招聘启事","校园招聘","校招公告","招聘简章","招聘信息","招聘计划","招聘项目","招聘专场","秋季招聘","春季招聘","招聘通知")
NAV_BAD=("登录","注册","首页","关于我们","新闻","公告","联系我们","隐私","下载","帮助","返回","信用卡产品","借记卡产品","自营金融","产品服务","理财","基金产品")
ROLE_HREF=("job","position","career","campus","recruit","zhaopin","jobs","vacancy","detail","post")
# Cohort evidence must come from visible text, not merely a site-level registry label.
COHORT_EVIDENCE_OK={"page_text","visible_announcement_title","job_detail_title","job_detail_text"}

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.text=[]; self.tables=[]; self.table_links=[]; self._row_links=[]; self._in_row=False; self.scripts=[]; self.page_title=""; self._in_title=False; self._script_buf=None; self._a=None; self._td=None; self._row=[]; self.headings=[]; self._heading_buf=None; self._heading_tag=None
    def handle_starttag(self,tag,attrs):
        d=dict(attrs); tag=tag.lower()
        if tag=="img" and d.get("alt"):
            alt=re.sub(r"\s+"," ",d.get("alt","")).strip()
            if alt: self.text.append(alt)
        if tag=="a":
            self._a={"href":d.get("href",""),"text":""}
            if self._in_row and d.get("href"): self._row_links.append(d.get("href",""))
        if tag=="title": self._in_title=True
        if tag in ("h1","h2","h3","h4"):
            self._heading_buf=[]; self._heading_tag=tag
        if tag in ("td","th"): self._td=""
        if tag=="tr": self._row=[]; self._row_links=[]; self._in_row=True
        if tag=="script": self._script_buf=[]
    def handle_data(self,data):
        s=re.sub(r"\s+"," ",data).strip()
        if s and self._script_buf is None: self.text.append(s)
        if self._a is not None: self._a["text"]+=(" "+s if s else "")
        if self._script_buf is not None: self._script_buf.append(data)
        if self._in_title: self.page_title+=data
        if self._heading_buf is not None: self._heading_buf.append(data)
        if self._td is not None: self._td+=(" "+s if s else "")
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag=="a" and self._a is not None: self.links.append(self._a); self._a=None
        if tag=="title": self._in_title=False
        if tag in ("h1","h2","h3","h4") and self._heading_buf is not None and tag==self._heading_tag:
            heading=re.sub(r"\s+"," ","".join(self._heading_buf)).strip()
            if heading: self.headings.append(heading)
            self._heading_buf=None; self._heading_tag=None
        if tag=="script" and self._script_buf is not None:
            body="".join(self._script_buf).strip()
            if body and len(body)<5000000: self.scripts.append(body)
            self._script_buf=None
        if tag in ("td","th") and self._td is not None:
            self._row.append(self._td.strip()); self._td=None
        if tag=="tr" and self._row:
            self.tables.append(self._row); self.table_links.append(self._row_links); self._in_row=False
    def page_text(self): return " ".join(self.text)

_BROWSER_WORKER=None
_BROWSER_WORKER_LOCK=threading.Lock()

def should_render_public_page(source_id,url,html):
    """Render only public SPA/list pages whose static HTML has no vacancy-detail links."""
    if source_id=="iguopin":
        return should_discover_public_url(source_id,url) and not is_direct_detail_url(url)
    if source_id=="51job":
        if not should_discover_public_url(source_id,url):
            return False
        parsed=Parser()
        parsed.feed(html or "")
        resolved=[canonical(urljoin(url,link.get("href",""))) for link in parsed.links if link.get("href")]
        if any(is_direct_detail_url(link) for link in resolved):
            return False
        return True
    return False

def render_public_page(url,source_id):
    """Ask a persistent anonymous Playwright worker to render a public page."""
    global _BROWSER_WORKER
    with _BROWSER_WORKER_LOCK:
        try:
            if _BROWSER_WORKER is None or _BROWSER_WORKER.poll() is not None:
                _BROWSER_WORKER=subprocess.Popen(
                    ["node",str(ROOT/"scripts/public_page_renderer.cjs")],
                    cwd=str(ROOT),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    text=True,encoding="utf-8",errors="replace",bufsize=1
                )
            render_url=url
            if source_id=="51job" and urlparse(url).scheme.lower()=="http" and same_host("https://51job.com/",url):
                p=urlparse(url)
                render_url=urlunparse(("https",p.netloc,p.path,p.params,p.query,p.fragment))
            request={"url":render_url,"source_id":source_id}
            _BROWSER_WORKER.stdin.write(json.dumps(request,ensure_ascii=False)+"\n")
            _BROWSER_WORKER.stdin.flush()
            line=_BROWSER_WORKER.stdout.readline()
            if not line:
                return {"ok":False,"error":"browser renderer exited without a response"}
            result=json.loads(line)
            return result if isinstance(result,dict) else {"ok":False,"error":"invalid renderer response"}
        except Exception as exc:
            return {"ok":False,"error":f"{type(exc).__name__}: {str(exc)[:120]}"}

def _shutdown_browser_worker():
    global _BROWSER_WORKER
    worker=_BROWSER_WORKER
    _BROWSER_WORKER=None
    if worker is None:
        return
    try:
        if worker.stdin:
            worker.stdin.close()
    except Exception:
        pass
    try:
        worker.terminate()
        worker.wait(timeout=3)
    except Exception:
        try: worker.kill()
        except Exception: pass

atexit.register(_shutdown_browser_worker)

def fetch(url,source_id=""):
    # Yingjiesheng's older public category URLs often circulate as HTTP links.
    # Prefer HTTPS, but if it fails at the transport/404 layer only, retry the
    # same allow-listed public URL over HTTP. Never fall back after 401/403/429.
    request_urls=[url]
    if source_id=="yingjiesheng" and same_host("https://www.yingjiesheng.com/",url):
        p=urlparse(url)
        if p.scheme.lower() in {"http","https"}:
            secure=urlunparse(("https",p.netloc,p.path,p.params,p.query,p.fragment))
            plain=urlunparse(("http",p.netloc,p.path,p.params,p.query,p.fragment))
            request_urls=list(dict.fromkeys((secure,plain)))
    last_error=None
    raw=None
    final=""
    enc="utf-8"
    for index,request_url in enumerate(request_urls):
        req=Request(request_url,headers=HEADERS)
        try:
            with urlopen(req,timeout=8 if source_id=="yingjiesheng" else 10) as r:
                try:
                    raw=r.read(4_000_000)
                except IncompleteRead as exc:
                    raw=exc.partial or b""
                    if not raw:
                        raise
                enc=r.headers.get_content_charset() or "utf-8"
                final=r.geturl()
            break
        except HTTPError as exc:
            if index+1<len(request_urls) and exc.code not in (401,403,429):
                last_error=exc
                continue
            raise
        except (URLError,TimeoutError) as exc:
            last_error=exc
            if index+1<len(request_urls):
                continue
            raise
    if raw is None:
        raise last_error or URLError("public page request returned no response")
    static_html=raw.decode(enc,errors="ignore")
    meta={"mode":"static","browser_attempted":False,"browser_success":False,
          "browser_blocked":False,"browser_body_chars":0,"browser_links":0,"browser_http_status":0,
          "browser_title":"","browser_final_url":"","browser_body_preview":"","browser_access_limited":False,
          "browser_error":""}
    if should_render_public_page(source_id,url,static_html):
        meta["browser_attempted"]=True
        result=render_public_page(url,source_id)
        if result.get("ok") and isinstance(result.get("html"),str) and result.get("html"):
            meta["mode"]="playwright"
            meta["browser_success"]=True
            meta["browser_access_limited"]=bool(result.get("access_limited"))
            meta["browser_blocked"]=bool(result.get("security_interstitial") or result.get("access_limited"))
            meta["browser_body_chars"]=int(result.get("body_chars") or 0)
            meta["browser_links"]=int(result.get("link_count") or 0)
            meta["browser_http_status"]=int(result.get("http_status") or 0)
            meta["browser_title"]=str(result.get("title") or "")[:160]
            meta["browser_final_url"]=str(result.get("final_url") or final)[:300]
            meta["browser_body_preview"]=str(result.get("body_preview") or "")[:350]
            rendered_url=str(result.get("final_url") or final)
            # A security wall is reported and skipped, never solved or bypassed.
            return str(result["html"]),rendered_url,meta
        meta["browser_error"]=str(result.get("error") or "browser render failed")[:180]
    return static_html,final,meta

def clean(s): return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()

DETAIL_JSON_KEYS=("jobCity","careerJobId","deliverBegin","deliverEnd","refreshBegin","latestProcessTime","graduationYear","salaryType","salaryMin","salaryMax","jobKeys","companyId","companyName","positionId","positionName","recruitJobName","jobName","jobOffer","eduLevel")

def sanitize_extracted_text(value, max_length=6500):
    """Remove embedded job-card JSON accidentally joined to visible detail text."""
    if not isinstance(value, str):
        return value
    text=value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
    text=text.replace("\\u002F", "/").replace("\\u002f", "/")
    text=re.sub(r"[ \t]+", " ", text).strip()
    keys = r"(?:jobCity|careerJobId|deliverBegin|deliverEnd|refreshBegin|latestProcessTime|graduationYear|salaryType|salaryMin|salaryMax|jobKeys|companyId|companyName|positionId|positionName|recruitJobName|jobName|jobOffer|eduLevel)"
    if re.match(r'''^\s*[\{\[]\s*(?:\\?["'])?''' + keys + r'''(?:\\?["'])?\s*:''', text, re.I):
        return ""
    property_key = re.compile(
        r'''[,;]\s*(?:\\?["'])?''' + keys + r'''(?:\\?["'])?\s*:''',
        re.I,
    )
    match=property_key.search(text)
    if match:
        text=text[:match.start()].rstrip(" \t,;\\")
        while text.endswith(('"', "'", "}", "]")):
            text=text[:-1].rstrip(" \t,;\\")
    return text[:max_length].strip()

def canonical(u):
    p=urlparse(u)
    # Preserve meaningful query parameters such as page=2 and jobId=123.
    # Strip analytics/tracking parameters but keep pagination and job identity.
    tracking={"utm_source","utm_medium","utm_campaign","utm_term","utm_content","spm","from","referrer","trackingid","property","requestid","policytype","policyid","pagecode","pagesource","isinitiative","issuggest","jobrank","advid"}
    query=sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in tracking)
    # urllib.request rejects raw Chinese characters in URL paths; percent-encode them.
    encoded_path=quote(p.path,safe="/:@()*+,;=-._~%")
    return urlunparse((p.scheme.lower(),p.netloc.lower(),encoded_path.rstrip("/"),"",urlencode(query,doseq=True),""))

def normalize_source_url(source_id,url):
    """Normalize discovered public listing URLs without inventing paths."""
    value=canonical(str(url or ""))
    p=urlparse(value)
    if source_id=="yingjiesheng" and p.scheme.lower()=="http" and same_host("https://www.yingjiesheng.com/",value):
        return urlunparse(("https",p.netloc,p.path,p.params,p.query,p.fragment))
    return value

def same_host(a,b):
    """Same host or approved recruitment-site subdomains (public pages only)."""
    ha=urlparse(a).netloc.lower().split(":")[0]
    hb=urlparse(b).netloc.lower().split(":")[0]
    if ha==hb: return True
    roots=("yingjiesheng.com","yingjiesheng.net","zhipin.com","nowcoder.com","iguopin.com","51job.com")
    root_a=next((root for root in roots if ha==root or ha.endswith("."+root)), "")
    root_b=next((root for root in roots if hb==root or hb.endswith("."+root)), "")
    return bool(root_a and root_a==root_b)

def should_discover_public_url(source_id,url):
    """Allow-list public listing/detail paths; never visit account, payment, or private areas."""
    p=urlparse(url); path=p.path.lower(); host=p.netloc.lower()
    if source_id=="yingjiesheng":
        if not same_host("https://www.yingjiesheng.com/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        if any(x in path for x in ("/login","/register","/member","/vip","/user/","/account","/privacy","/thirdlink","/redirect")): return False
        return (path=="/" or path.startswith(("/major/","/other/","/jobs/","/job/","/joblist/","/xuanjianghui/"))
                or host.startswith(("zhiwei.","q.","web.","s."))
                or bool(re.search(r"/jobdetail/\d+",path))
                or bool(re.search(r"/job-[a-z0-9-]+\.html$",path))
                or bool(re.search(r"/xjh-[a-z0-9-]+\.html$",path))
                or any(k in p.query.lower() for k in ("page=","pageno=","pagenum=","pageindex=")))
    if source_id=="boss":
        if not same_host("https://www.zhipin.com/school/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        if any(x in path for x in ("/login","/register","/user/","/account","/vip","/app/")): return False
        # Do not treat /web/geek/job?query=... as a vacancy or campus list.
        # That route is a general consumer search/category page and produced
        # misleading rows such as "后端开发" with the company set to BOSS itself.
        return (path.rstrip("/") in {"/school","/xiaoyuan"}
                or path.startswith("/school/") or path.startswith("/xiaoyuan/")
                or path.startswith("/job_detail/"))
    if source_id=="nowcoder":
        if not same_host("https://www.nowcoder.com/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        return path.startswith("/jobs/") or path.startswith("/job/") or any(k in p.query.lower() for k in ("page=","pageindex="))
    if source_id=="iguopin":
        if not same_host("https://job.iguopin.com/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        if any(x in path for x in ("/login","/register","/resume","/member","/account","/app")): return False
        return (path in {"/","/job","/joblist","/jobdetail","/job-detail","/job/detail"}
                or path.startswith(("/job/","/joblist/","/jobdetail/","/job-detail/","/job/detail/","/position/","/positions/","/campus/","/article/","/news/"))
                or any(k in p.query.lower() for k in ("channel=campus","channel=social","page=","pageindex=","pageno=","jobid=","positionid=","keyword=","experience=","education=")))
    if source_id=="51job":
        if not same_host("https://xy.51job.com/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        if any(x in path for x in ("/login","/register","/resume","/member","/account","/app/")): return False
        host=urlparse(url).netloc.lower()
        # Public employer microsites often link company-profile pages ending in
        # .html. They are not vacancies and used to pollute the rotating pool.
        # Keep campus microsites for listing discovery, and only accept true
        # numeric job-detail pages under jobs./company. hosts.
        if host.startswith("campus."): return True
        if host.startswith(("jobs.","company.")): return is_direct_detail_url(url)
        if not host.startswith("xy."): return False
        allowed_path=(path in {"/","/default-xs.php","/default.php"}
                      or path.startswith(("/jobs/","/job/","/campus/","/position/","/positions/","/jobdetail/","/job-detail/"))
                      or path.endswith((".html",".php")))
        allowed_query=any(k in p.query.lower() for k in ("page=","pageno=","jobid=","positionid=","keyword=","lang=e"))
        return allowed_path or allowed_query
    return False

def diagnostic_link_priority(source_id,url,title=""):
    """Rank diagnostic links: vacancy detail first, allowed recruitment routes next, navigation last."""
    if is_direct_detail_url(url):
        return 0
    if should_discover_public_url(source_id,url):
        return 1
    path=urlparse(url or "").path.lower()
    text=(str(title or "")+" "+path+" "+urlparse(url or "").query).lower()
    hints=("job","position","vacan","career","campus","recruit","zhaopin","school","jobs","joblist","职位","岗位","招聘","校招","应届")
    if any(hint in text for hint in hints):
        return 2
    return 9

def discovery_priority(source_id,url):
    """Prefer public list/category/pagination links while retaining direct details."""
    p=urlparse(url); path=p.path.lower(); query=p.query.lower()
    if source_id=="yingjiesheng":
        if re.search(r"(?:[?&](?:page|pageno|pagenum|pageindex)=\d+|/(?:index|joblist|list)[_-]?\d+\.html)",query+" "+path): return 0
        if "/major/" in path or "/other/" in path or path=="/" or p.netloc.lower().startswith(("zhiwei.","q.","web.","s.")): return 1
        if is_direct_detail_url(url): return 2
        if "/xjh-" in path or "/job-" in path: return 3
        return 4
    if source_id=="boss":
        if path.rstrip("/") in ("/school","/xiaoyuan") or path.startswith("/school/") or path.startswith("/xiaoyuan/"): return 0
        if is_direct_detail_url(url): return 2
        return 4
    if source_id=="nowcoder":
        if is_direct_detail_url(url): return 2
        return 0
    if source_id=="iguopin":
        if re.search(r"(?:[?&](?:page|pageindex|pageno)=\d+)",query): return 0
        if is_direct_detail_url(url): return 2
        return 1 if path in {"/","/joblist"} else 4
    if source_id=="51job":
        if re.search(r"(?:[?&](?:page|pageno)=\d+)",query): return 0
        if is_direct_detail_url(url): return 2
        return 1 if path in {"/","/default-xs.php","/default.php"} else 4
    return 5

def year(s):
    """Return a cohort only when visible text explicitly provides one; never guess 2027."""
    text=s or ""
    m=re.search(r"(?<!\d)(20\d{2})\s*届",text)
    if m: return m.group(1)+"届"
    m=re.search(r"(?<!\d)(2[5-9])\s*届",text)
    if m: return "20"+m.group(1)+"届"
    # Recruitment titles often say "2027校园招聘/2027校招" without the 届 suffix.
    m=re.search(r"(?<!\d)(20\d{2})(?:年)?\s*(?:校园招聘|校招公告|校招职位|秋季招聘|秋招|春招)",text)
    if m: return m.group(1)+"届"
    m=re.search(r"(?<!\d)(20\d{2})\s*年(?:应届|毕业)",text)
    if m: return m.group(1)+"届"
    return ""
def category(t):
    t=t.lower()
    if any(x in t for x in ("ai","算法","大模型","机器学习","软件","开发","工程师","数据","技术","研发","嵌入式")): return "AI / 算法 / 技术"
    if any(x in t for x in ("产品","运营","增长")): return "产品 / 运营"
    if any(x in t for x in ("设计","视觉","交互","ux","ui")): return "设计"
    if any(x in t for x in ("销售","商务","营销","品牌","市场")): return "销售 / 商务 / 营销"
    if any(x in t for x in ("银行","金融","投资","风险","财务","审计","fintech")): return "金融 / 银行"
    if any(x in t for x in ("人力","hr","招聘")): return "人力资源"
    if any(x in t for x in ("供应链","制造","采购","物流","机械","电气")): return "供应链 / 制造"
    if any(x in t for x in ("法律","法务","合规")): return "法务 / 合规"
    return "综合 / 校园招聘"
def make_id(company,title,url): return "auto-"+hashlib.sha1((company+"|"+title+"|"+canonical(url)).encode()).hexdigest()[:16]

def score(title,href,context):
    s=0; t=(title+" "+href).lower()
    if any(w.lower() in title.lower() for w in CONCRETE_WORDS): s+=5
    if any(w.lower() in href.lower() for w in ROLE_HREF): s+=2
    if re.search(r"20\d{2}",title): s+=1
    if any(w.lower() in title.lower() for w in PROGRAM_WORDS): s-=4
    if any(w.lower() in title.lower() for w in NAV_BAD): s-=6
    if len(title)>70: s-=2
    return s

def is_direct_detail_url(url):
    p=urlparse(url or "")
    path=p.path.lower()
    if re.search(r"/jobs/detail/\d+",path): return True
    if re.search(r"/jobs/detail/(?:graduate|intern)/[0-9a-f-]{20,}",path): return True
    if re.search(r"/jobs/hr/\d+",path) and re.search(r"(?:^|&)jobid=\d+",p.query.lower()): return True
    if re.search(r"/jobdetail/\d+",path): return True
    if path.endswith("/jobdesc.html") and re.search(r"(?:^|&)postid=[^&]+",p.query.lower()): return True
    if path.endswith("/web/position/detail") and re.search(r"(?:^|&)jobunionid=\d+",p.query.lower()): return True
    if re.search(r"/campus/position/\d+/detail",path): return True
    if re.search(r"/position/\d+/detail",path): return True
    if re.search(r"/positions/\d+(?:/|$)",path): return True
    if re.search(r"/job/detail/\d+",path): return True
    if re.search(r"/job/position/\d+",path): return True
    if re.search(r"/job-\d{3}-\d{3}-\d+\.html$",path): return True
    if re.search(r"/job_detail/[^/]+(?:\.html)?$",path): return True
    # Guopin detail pages require an explicit identity; jobList is not a vacancy.
    if path in {"/jobdetail","/job-detail","/job/detail"} and re.search(r"(?:^|&)(?:jobid|positionid|id)=\w+",p.query.lower()): return True
    if re.search(r"/(?:jobdetail|job-detail|positions?)/[^/]+(?:\.html)?$",path): return True
    # Legacy 51job detail URLs use a numeric vacancy id in the filename.
    if p.netloc.lower().endswith("51job.com") and re.search(r"/\d{6,}\.html$",path): return True
    if re.search(r"/jobs/position-\d+(?:/|$)",path): return True
    return False

TITLE_NOISE = re.compile(r"(?:\d+\s*分钟前在线|HR\s*反馈率|反馈率\s*[:：]?|反馈时长\s*[:：]?|招聘经理|人事经理|招聘专员|今日活跃|刚刚活跃)",re.I)
ROLE_TERMS=("AIGC","AI产品","AI应用","视频","影视","创意制作","制作","创作","动画","短视频","剪辑","编导","文案","游戏","直播","摄影","工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","设计","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","分析师","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理","策划","内容","新媒体","客服","行政","人事","风控","信贷","会计","策略","助理","客户经理","技术支持","商业分析","投资","证券","保险","精算","编辑","翻译","品牌","公关","电商","市场拓展","芯片","硬件","软件","制造","HR","Engineer","Designer","Analyst","Product","Operations")

def clean_job_title(title, source_id=""):
    """Remove cohort chips, platform metadata and HR-status pollution from titles."""
    t=clean(title)
    t=re.sub(r"^\s*[【〖\[（(]\s*(?:20\d{2}|\d{2})\s*届\s*(?:校招|校园招聘|正式批|秋招)?\s*[】〗\]）)]\s*[-—:：_ ]*", "", t)
    t=re.sub(r"^\s*(?:20\d{2}|\d{2})\s*届(?:校招|校园招聘)?\s*[-—:：_ ]*", "", t)
    if source_id=="nowcoder":
        m=re.match(r"^(.+?)_([^_]+?)(?:校招|实习|社招)_牛客网$",t)
        if m: t=m.group(1)
        t=re.sub(r"^(?:20\d{2}|\d{2})届(?:校招)?[-—_\s]*","",t)
        t=re.sub(r"^\s*(?:收藏|已收藏)\s*","",t)
        # Remove card-only labels and HR/salary metadata from visible title text.
        t=re.split(r"\s+(?=(?:\d{2,4}(?:[-—~至]\d{2,4})?元/天|薪资(?:超|面议|[:：])|HR(?:刚|今日|今天|在线)|助力简历|直达官网|投后必反馈|校招高薪榜|最少\d+个月|\d+天/周|地点[：:]|\d{2,5}[-–]\d{2,5}人|有转正|企业服务|全场景覆盖|数字化运营))",t,maxsplit=1)[0]
        if re.search(r"(?:有限公司|股份|集团|科技|研究院|大学|银行).{0,24}\s+(?:互联网|机械制造|电商|金融|教育|企业服务|游戏|电子商务)\s*$",t):
            t=""
    if source_id=="yingjiesheng" and "招聘_" in t:
        t=t.split("招聘_",1)[0].strip()
    if source_id=="boss":
        t=re.sub(r"\s*[-—|｜_]\s*BOSS直聘.*$","",t,flags=re.I)
        t=re.sub(r"\s*招聘(?:信息)?[-_ ]*BOSS直聘.*$","",t,flags=re.I)
        t=re.sub(r"^(?:20\d{2}[A-Z]{0,6}|20\d{2}|\d{2}届)[-—:：\s]*","",t)
        t=re.sub(r"^(?:校招末班车|顶尖应届|急招)[-—:：\s]*","",t)
        t=re.sub(r"[-—](?:多赛道发展|高管直带).*$","",t)
        # Public BOSS job cards append city, seniority, degree, skills and online-work labels after the role.
        # Remove the first clearly labelled location/experience/degree segment; this keeps role names specific.
        t=re.sub(r"[（(](?:北京|上海|深圳|广州|杭州|南京|成都|西安|武汉|苏州|合肥|重庆|天津|青岛|济南|全国|远程|在线)[）)]","",t)
        cities="北京|上海|深圳|广州|杭州|南京|成都|西安|武汉|苏州|合肥|重庆|天津|青岛|济南|长沙|东莞|厦门|福州|郑州|宁波|无锡|南昌|哈尔滨|全国|远程|在线"
        city_hit=re.search(r"\s+(?:"+cities+r")(?:\s|$)",t)
        if city_hit: t=t[:city_hit.start()]
        else:
            meta_hit=re.search(r"\s+(?:在校/应届|应届毕业生|应届生|经验不限|\d+年以内|\d+[-—至]\d+年|本科(?:及以上)?|硕士(?:及以上)?|博士(?:及以上)?|大专(?:及以上)?|薪资面议|通信相关专业|计算机相关专业|自然语言处理|深度学习|机器学习|算法经验|开发经验|B端产品|大模型训练框架)\b.*$",t)
            if meta_hit: t=t[:meta_hit.start()]
        t=re.sub(r"\s*[-—]\s*(?:"+cities+r")\s*$","",t)
        t=t.strip(" -—_|｜")
    if source_id=="51job":
        t=re.sub(r"\s*[-—|｜_]\s*(?:前程无忧|51job).*$","",t,flags=re.I)
        t=re.sub(r"\s*[-—|｜_]\s*校园招聘.*$","",t)
    if source_id=="iguopin":
        t=re.sub(r"\s*[-—|｜_]\s*国聘.*$","",t)
    if source_id=="qiuzhaowang":
        t=re.sub(r"\s*[|｜丨]\s*秋招网.*$","",t)
        # The generic cleaner strips a leading cohort chip first, so this pattern
        # intentionally works with or without the year prefix.
        t=re.sub(r"^(?:20\d{2}届)?最新更新校招岗位.*$","",t)
    t=re.sub(r"\s*[_|｜]\s*(?:牛客网|应届生求职网).*$","",t)
    t=re.sub(r"\s*[-—]\s*(?:牛客网|应届生求职网).*$","",t)
    t=re.sub(r"\s*\d+\s*分钟前在线.*$","",t)
    t=re.sub(r"\s*[·|｜]\s*HR\s*反馈率.*$","",t,flags=re.I)
    t=re.sub(r"\s*(?:HR\s*)?反馈率\s*[:：]?.*$","",t,flags=re.I)
    t=re.sub(r"\s*反馈时长\s*[:：]?.*$","",t,flags=re.I)
    t=re.sub(r"\s*(?:招聘经理|人事经理|招聘专员).*$","",t)
    t=re.sub(r"^(?:招聘岗位|职位|岗位名称)\s*[:：]\s*","",t)
    t=re.sub(r"\s*[（(]\s*[A-Z]{1,4}\d{4,}\s*[）)]\s*$","",t,flags=re.I)
    t=re.sub(r"\s+"," ",t).strip(" -—_|｜")
    return t

def is_concrete_role_title(title, source_id=""):
    t=clean_job_title(title,source_id)
    if len(t)<2 or len(t)>90 or TITLE_NOISE.search(t): return False
    if any(w in t for w in ("招聘公告","招聘简章","招聘计划","校园招聘会","招聘宣讲","专业目录","需求专业","岗位要求","任职要求","职位描述","职位亮点","公司信息","查看更多","点击查看","招聘信息","福利待遇","工作环境","员工评价","职位搜索","职位列表","岗位列表","招聘官网","校园招聘官网","加入我们","最新更新","分类求职","岗位汇总","岗位清单","职位分类","岗位分类","招聘岗位与截止时间","校招职位")): return False
    major_only = {
        "计算机科学与技术","软件工程","机械工程","材料科学与工程","化学工程与工艺",
        "电子信息工程","自动化","商务英语","国际贸易","英语","金融学","会计学",
        "工商管理","市场营销","人力资源管理","物流管理","法学","新闻学","广告学",
        "视觉传达设计","数字媒体艺术","工业设计","数学与应用数学","统计学",
        "电气工程及其自动化","通信工程","土木工程","建筑学","环境工程","生物工程",
        "护理学","药学","临床医学","汉语言文学","行政管理","电子商务",
        "戏剧与影视学","安全科学与工程","电气工程","航空航天工程","生物医学工程",
        "电子科学与技术","金融工程","新闻传播学","应用数学","信息管理与信息系统"
    }
    base=re.sub(r"[（(](?:专业不限|相关专业|应届生优先|接受应届生)[）)]$","",t)
    base=re.sub(r"(?:专业不限|等相关专业|相关专业)$","",base).strip()
    if base in major_only: return False
    return any(w.lower() in t.lower() for w in ROLE_TERMS)

def is_specific_role_title(title, source_id=""):
    t=clean_job_title(title,source_id)
    if not is_concrete_role_title(t,source_id): return False
    if re.search(r"(?:最新更新|招聘信息|截止时间|职位列表|岗位列表|职位分类|岗位分类|专业目录)",t): return False
    if any(noise in t for noise in ("收藏","立即投递","官网投递","天截止","届秋招","全生态布局","龙头企业","内容与技术能力领先","职位推荐","职位精选","职场优选","企业墙","地点：","地点:","校招高薪榜","薪资超","HR刚","HR今日","HR在线","助力简历","投后必反馈","全场景覆盖","数字化运营与服务能力突出")): return False
    stripped=re.sub(r"(?:专业不限|可接受应届生|接受应届生|应届生优先|相关专业)$","",t).strip()
    bare=re.sub(r"[（(](?:校招|校园招聘|实习生?|应届生?|应届毕业生|20\d{2}届|\d{2}届)[）)]","",stripped)
    bare=re.sub(r"(?:校招|校园招聘|实习生?|应届生?)$","",bare).strip()
    # A role should be granular enough to distinguish the actual job from a
    # major/discipline heading. Do not publish bare category words as vacancy names.
    generic_only={"工程师","设计师","设计","销售","研发","开发","算法","数据","产品","运营","财务","市场","客服","行政","人事","专员","助理","管培生","管理培训生","经理","分析师","咨询","研究员","翻译","编辑","机械","电气","物流","技术","工程","管理","培训生"}
    return len(stripped)>=2 and bare not in generic_only and not stripped.endswith(("类","方向","体系","专业"))

def clean_yingjiesheng_company_name(value):
    company=clean(value or "")
    company=re.sub(r"^\s*[\[【（(][^\]】）)]{1,15}[\]】）)]\s*","",company)
    company=re.sub(r"\s*(?:招聘信息|校园招聘|招聘启事)$","",company)
    return company.strip(" -—_|｜")

def split_role_title_candidates(value,source_id=""):
    """Split multi-role detail headings into separate role-level records."""
    text=clean_job_title(value,source_id)
    parts=[clean_job_title(part,source_id) for part in re.split(r"\s*[|｜丨、；;]\s*",text) if part.strip()]
    result=[]
    for part in parts:
        if is_specific_role_title(part,source_id) and part not in result:
            result.append(part)
    return result

def is_boss_campus_card_relevant(raw_title):
    """Keep specific BOSS campus leads but discard clear non-graduate/old-cycle cards."""
    raw=clean(raw_title or "")
    title=clean_job_title(raw,"boss")
    if not is_specific_role_title(title,"boss"):
        return False
    year_marker=(re.search(r"(?<!\d)(?:20(?:2[0-6])|2[0-6])\s*(?:(?:届|年)\s*(?:应届|毕业生?|校招|校园招聘|秋招|春招)?|校招|校园招聘|秋招|春招)",raw) or re.search(r"(?<!\d)(?:20(?:2[0-6])|2[0-6])\s*(?:(?:届|年)\s*)?[^，,。;；]{0,18}(?:校招|校园招聘|秋招|春招)",raw))
    cohort=year(raw)
    if (year_marker or (cohort and cohort!="2027届")) and not re.search(r"(?<!\d)2027\s*(?:届|年)?\s*(?:校招|校园招聘|秋招|春招)|(?<!\d)27\s*(?:届|校招|校园招聘|秋招|春招)",raw):
        return False
    positive=bool(re.search(r"(?<!\d)2027\s*(?:届|年)?\s*(?:校招|校园招聘|秋招|春招)|(?<!\d)27\s*(?:届|校招|校园招聘|秋招|春招)|应届|在校/应届|应届毕业生|毕业生",raw))
    if re.search(r"(?:3\s*[-—至]\s*5|5\s*[-—至]\s*10|10\s*年以上)\s*年",raw) and not positive:
        return False
    if re.search(r"(?:司机|店员|保洁|保安|传菜员|服务员|日结|小时工|外卖骑手)",raw) and not positive:
        return False
    if "兼职" in raw and not positive:
        return False
    return True

def should_queue_detail(source_id,url):
    """Avoid repeated BOSS detail requests after public cards are blocked by security walls."""
    return is_direct_detail_url(url) and source_id!="boss"

def extract_qiuzhaowang_role_names(text, link_hint=""):
    """Extract concrete QZW job titles from visible detail text or its public listing card."""
    text=clean(text or "")
    candidates=[]
    patterns=(
        r"(?:招聘岗位|招聘职位|岗位名称|职位名称)\s*[:：]?\s*(.{1,260}?)(?=\s*(?:工作城市|工作地点|工作地区|面向届次|届次批次|招聘项目|岗位职责|岗位要求|任职要求|职位描述|企业介绍|公司信息|$))",
        r"(?:招聘岗位|招聘职位)\s*[:：]?\s*(.{1,160}?)(?=\s*$)",
    )
    for pattern in patterns:
        match=re.search(pattern,text)
        if match:
            candidates.append(match.group(1))
            break
    if not candidates and link_hint:
        candidates.append(clean(link_hint))
    result=[]
    role_starts=("AIGC","AI","FPGA","大数据","算法","研发","软件","应用","嵌入式","人力资源","供应链","销售","财务","项目","产品","技术","工程","质量","管理","采购","市场","运营","数据","机械","电气","材料","设计","法律","法务","审计","教师","研究","客户","生产","工艺","医学","药学","营销","品牌","战略","风险","金融","投资","管培","行政","专员","计算机","通信","网络","硬件","芯片","自动化","控制","系统","测试","运维","服务","助理","顾问","咨询","业务","商务","物流","设备","计划","渠道","会计","客服","机器人","分析","编辑","翻译","内容","直播","动画","影视","剪辑","编导","策划")
    for raw in candidates:
        raw=re.sub(r"(?:招聘项目包含.*$|(?:\.\.\.|…)+$)","",raw).strip()
        parts=[part.strip(" -—:：") for part in re.split(r"\s*[·•｜|丨、；;]\s*",raw) if part.strip()]
        if len(parts)==1:
            parts=re.split(r"\s+(?=(?:"+"|".join(map(re.escape,role_starts))+r"))",raw)
        for part in parts:
            title=clean_job_title(part,"qiuzhaowang")
            if 2<len(title)<=45 and is_specific_role_title(title,"qiuzhaowang") and title not in result:
                result.append(title)
    return result[:40]

def select_rotation_batch(pool, cursor, limit):
    """Rotate only through discovered public listing URLs; never invent page URLs."""
    unique=[]
    seen=set()
    for url in pool or []:
        value=canonical(str(url))
        if value and value not in seen:
            unique.append(value); seen.add(value)
    if not unique or int(limit)<=0:
        return [],max(0,int(cursor or 0))
    offset=max(0,int(cursor or 0))
    start=offset % len(unique)
    count=min(len(unique),max(1,int(limit)))
    return [unique[(start+i)%len(unique)] for i in range(count)],offset+count

def extract_section(text, starts, ends, limit=5000):
    start_re="(?:"+"|".join(starts)+r")\s*[:：]?\s*"
    m=re.search(start_re,text or "",re.I)
    if not m: return ""
    tail=(text or "")[m.end():]
    positions=[]
    for end in ends:
        hit=re.search(r"\s*(?:"+end+r")\s*[:：]?\s*",tail,re.I)
        if hit: positions.append(hit.start())
    if positions: tail=tail[:min(positions)]
    return re.sub(r"\s+"," ",tail).strip(" :-—")[:limit].strip()

def extract_yingjiesheng_fields(raw_title, text, url):
    """Extract visible fields from public Yingjiesheng job-detail pages."""
    fields={}; title=""; company=""
    path=urlparse(url).path.lower()
    if "/jobdetail/" in path:
        if "招聘_" in raw_title:
            title,rest=raw_title.split("招聘_",1)
            title=title.strip()
            company=re.split(r"招聘信息|_应届生求职网",rest,1)[0].strip(" _-")
        else:
            title=raw_title.split("_",1)[0].strip()
    else:
        # Older Yingjiesheng mobile detail pages often put province/company/role/date
        # directly in the HTML title instead of exposing the same structured fields.
        old_title=re.match(r"^\[([^\]]{1,24})\]\s*(.+?)\s+(20\d{2}-\d{1,2}-\d{1,2})$",clean(raw_title))
        if old_title:
            entry=old_title.group(2).strip()
            fields["publish_date"]=old_title.group(3)
            company_role=re.match(
                r"^(.+?(?:股份有限公司|集团有限公司|有限公司|集团|研究院|研究所|大学|银行|医院|中心|公司))\s+(.+)$",
                entry
            )
            if company_role:
                company=company_role.group(1).strip()
                title=company_role.group(2).strip()
            else:
                title=entry
        m=re.search(r"职位\s*[:：]\s*(.+?)\s+发布时间",text or "")
        if m: title=m.group(1).strip() or title
        m=re.search(r"\[(?:全国|北京|上海|广州|深圳|杭州|南京|成都|武汉|西安)[^\]]*\]\s*(.+?)\s+(?:职位\s*[:：]|招聘岗位)",text or "")
        if m and not company: company=m.group(1).strip()
        if not company:
            m=re.search(r"(?:^|\s)([^\s]{2,40}有限公司)\s+(?:职位\s*[:：]|招聘岗位)",text or "")
            if m: company=m.group(1).strip()
    role_titles=split_role_title_candidates(title,"yingjiesheng")
    fields["role_titles"]=role_titles
    fields["title"]=role_titles[0] if role_titles else clean_job_title(title,"yingjiesheng")
    fields["company"]=clean_yingjiesheng_company_name(company)
    top=(text or "")[:3500]
    annual_salary=re.search(r"(?:年薪\s*)?\d+(?:\.\d+)?\s*(?:万元|万|元)(?:\s*/\s*年|/年|每年)",top)
    if annual_salary:
        fields["salary"]=re.sub(r"\s+","",annual_salary.group(0))
    else:
        rmb_salary=re.search(r"\d[\d,]*(?:\s*[-—~～至到]\s*\d[\d,]*)?\s*元\s*/\s*(?:月|天|年)",top)
        if rmb_salary:
            fields["salary"]=re.sub(r"\s+","",rmb_salary.group(0))
        else:
            salary=re.search(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|每月|月)?",top)
            if salary: fields["salary"]=re.sub(r"\s+","",salary.group(0))
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    # City names in site navigation/related cards are not reliable work locations.
    # Extract only the explicit location label from this job detail.
    cities=("上海","北京","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","珠海","乌鲁木齐","洛阳","保定","廊坊","中山","佛山","温州","嘉兴","金华","常州","徐州","大连","沈阳","长春","石家庄","太原","南宁","贵阳","昆明","兰州","西宁","银川","呼和浩特","海口","三亚","泉州","汕头","惠州","江门","肇庆","清远","芜湖","扬州","南通","镇江","泰州","烟台","潍坊","淄博","临沂","济宁","日照","威海","唐山","秦皇岛","邯郸","开封","新乡","安阳","许昌","南阳","宜昌","襄阳","荆州","株洲","湘潭","衡阳","岳阳","绵阳","德阳","宜宾","泸州","乐山","遵义","大庆","齐齐哈尔","包头","鄂尔多斯","克拉玛依","喀什","阿克苏","伊犁","拉萨","林芝")
    location_match=re.search(r"(?:工作地点|工作城市|工作地区|工作地址)\s*[:：]?\s*(.{1,80}?)(?=\s*(?:信息来源|职位类型|专业标签|招聘岗位|学历要求|学历|职位描述|岗位职责|职位诱惑|公司信息|企业信息|$))",top,re.I)
    if location_match:
        location_text=location_match.group(1).strip()
        if location_text in ("其它","其他"):
            fields["city"]="其它"
        elif location_text in ("全国","全国各地","不限"):
            fields["city"]="全国"
        else:
            found=[name for name in cities if re.search(re.escape(name)+r"(?:市|区|\b|$)",location_text)]
            if found:
                fields["city"]="、".join(dict.fromkeys(found))
    pub=re.search(r"发布时间\s*[:：]?\s*(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?",top)
    if pub: fields["publish_date"]=f"{pub.group(1)}-{int(pub.group(2)):02d}-{int(pub.group(3)):02d}"
    deadline=re.search(r"(?:截止时间|报名截止|投递截止|截止日期)\s*[:：]?\s*(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?",text or "")
    if deadline: fields["deadline"]=f"{deadline.group(1)}-{int(deadline.group(2)):02d}-{int(deadline.group(3)):02d}"
    fields["benefits"]=extract_section(text,("职位亮点","职位诱惑","福利待遇","薪酬福利"),("职位描述","岗位职责","工作职责","职位要求","任职要求","岗位要求","公司信息","单位简介"),1600)
    fields["responsibilities"]=extract_section(text,("岗位职责","工作职责","工作内容"),("岗位要求","任职要求","任职资格","加分项","职位亮点","职位诱惑","工作时间","公司信息","单位简介"),3500)
    fields["requirements"]=extract_section(text,("岗位要求","任职要求","任职资格"),("加分项","职位亮点","职位诱惑","工作时间","工作地址","公司地址","公司信息","单位简介","投递说明","上一条","下一条"),3500)
    fields["description"]=extract_section(text,("职位描述","招聘岗位"),("公司信息","单位简介","上一条","下一条","Top"),7000)
    fields["company_info"]=extract_section(text,("公司信息","单位简介"),("上一条","下一条","登录","Top"),1800)
    addr=re.search(r"(?:公司地址|工作地址|地址)\s*[:：]?\s*(.{4,120}?)(?=\s*(?:投递简历|职位亮点|职位描述|公司信息|单位简介|上一条|下一条|Top|$))",text or "",re.I)
    if addr: fields["address"]=addr.group(1).strip()
    for label in ("实习","全职","兼职","校招"):
        if label in top:
            fields["job_type"]=label; break
    exp=re.search(r"(?:工作经验|经验要求|经验)\s*[:：]?\s*(不限|无经验|\d+[-—至]\d+年|\d+年以上|应届毕业生)",top)
    if exp: fields["experience"]=exp.group(1)
    headcount=re.search(r"(?:招|招聘人数\s*[:：]?)\s*(\d+)\s*人",top)
    if headcount: fields["headcount"]=headcount.group(1)
    return fields

def extract_detail_fields_generic(text, raw_title=""):
    fields={}
    top=(text or "")[:4500]
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    salary=re.search(r"(?:薪资|月薪)\s*[:：]?\s*(\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|月)?|薪资面议)",top)
    if not salary: salary=re.search(r"\b(\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|月)?)",top)
    if salary: fields["salary"]=re.sub(r"\s+","",salary.group(1))
    cities=("北京","上海","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外")
    # Preserve every city shown on a role detail page. A multi-city job like
    # "北京市,上海市" must remain discoverable when users filter Shanghai.
    found_cities=[]
    for name in cities:
        pattern=r"(?:^|[\s,，、/|·\-:：省])"+re.escape(name)+r"(?:$|[\s,，、/|·\-:：区市省])"
        if re.search(pattern,top[:1800]) and name not in found_cities:
            found_cities.append(name)
    if found_cities: fields["city"]="、".join(found_cities)
    cohort=year(raw_title+" "+top)
    if cohort:
        fields["cohort"]=cohort; fields["cohort_evidence"]="page_text"; fields["cohort_confirmed"]=True
    dr=re.search(r"投递时间\s*[:：]?\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*[-—至到]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日",text or "")
    if dr: fields["deadline"]=f"{dr.group(4)}-{int(dr.group(5)):02d}-{int(dr.group(6)):02d}"
    fields["responsibilities"]=extract_section(text,("岗位职责","工作职责","工作内容"),("岗位要求","任职要求","职位亮点","职位描述","公司信息","上一条","下一条"),3500)
    fields["requirements"]=extract_section(text,("岗位要求","任职要求","任职资格"),("加分项","职位亮点","工作时间","公司信息","上一条","下一条"),3500)
    fields["benefits"]=extract_section(text,("职位亮点","职位诱惑","福利待遇"),("岗位职责","工作职责","职位描述","公司信息","上一条","下一条"),1800)
    fields["description"]=extract_section(text,("职位描述","岗位职责"),("公司信息","上一条","下一条","Top"),6500)
    fields["company_info"]=extract_section(text,("公司信息","公司介绍"),("查看其他","上一条","下一条","Top"),1800)
    addr=re.search(r"(?:公司地址|工作地址|地址)\s*[:：]?\s*(.{4,120}?)(?=\s*(?:投递简历|职位亮点|职位描述|公司信息|上一条|下一条|Top|$))",text or "",re.I)
    if addr: fields["address"]=addr.group(1).strip()
    exp=re.search(r"(?:工作经验|经验要求|经验)\s*[:：]?\s*(不限|无经验|\d+[-—至]\d+年|\d+年以上|应届毕业生)",top)
    if exp: fields["experience"]=exp.group(1)
    if "全职" in top: fields["job_type"]="全职"
    elif "实习" in top: fields["job_type"]="实习"
    return fields

def extract_boss_card_company(links, role_index, page_url=""):
    """Infer a company name from adjacent visible BOSS card links, without guessing from role text."""
    company_tags=("计算机软件","智能硬件","电子商务","互联网","企业服务","人力资源服务","医疗健康","金融","教育培训","通信/网络设备","机器人","工业自动化","软件和信息技术服务业","信息安全","已上市","不需要融资","A轮","B轮","C轮","D轮","天使轮","人以上")
    blocked={"boss直聘","校园招聘","查看更多","更多","公司","职位","实习","校招","应届生求职网"}
    indices=list(range(role_index+1,min(len(links),role_index+5)))+list(range(role_index-1,max(-1,role_index-4),-1))
    for idx in indices:
        link=links[idx]
        text=clean(link.get("text",""))
        href=urljoin(page_url,link.get("href",""))
        if not text or is_direct_detail_url(href) or text.lower() in blocked: continue
        if len(text)>100 or any(x in text for x in ("HR反馈","立即投递","职位描述","查看更多岗位")): continue
        candidate=text
        tag_match=re.search(r"\s+(?:"+"|".join(map(re.escape,company_tags))+r")(?:\s|$)",candidate)
        if tag_match:
            candidate=candidate[:tag_match.start()].strip()
        candidate=re.sub(r"\s+(?:已上市|不需要融资|A轮|B轮|C轮|D轮(?:及以上)?|天使轮|\d{1,5}(?:-\d{1,5})?人(?:以上)?).*$","",candidate).strip()
        candidate=re.sub(r"\s+(?:在校/应届|应届|本科|硕士|博士|大专|互联网|智能硬件|企业服务).*$","",candidate).strip()
        if not (2<=len(candidate)<=60) or candidate.lower() in blocked: continue
        # Strong company-like signals win; otherwise use a short adjacent name only if it is not itself a role title.
        if re.search(r"(?:有限公司|股份|集团|科技|大学|研究院|银行|证券|公司|中心|网络|智能|生物|机械|电子|汽车)$",candidate):
            return candidate
        if tag_match and not is_specific_role_title(candidate,"boss"):
            return candidate
        if len(candidate.split())==1 and not is_specific_role_title(candidate,"boss"):
            return candidate
    return ""

def extract_boss_card_fields(raw_title, links, role_index, page_url=""):
    """Parse only fields visibly present on a public BOSS vacancy card."""
    raw=clean(raw_title)
    title=clean_job_title(raw,"boss")
    if not is_boss_campus_card_relevant(raw_title):
        return {}
    company=extract_boss_card_company(links,role_index,page_url)
    if not company:
        return {}
    fields={"title":title,"company":company}
    fields.update({k:v for k,v in extract_detail_fields_generic(raw,raw).items() if v not in ("",None)})
    hourly=re.search(r"(?<!\d)(\d+(?:\.\d+)?\s*[-—~至]\s*\d+(?:\.\d+)?\s*元/天)",raw)
    if hourly: fields["salary"]=re.sub(r"\s+","",hourly.group(1))
    cohort=year(raw)
    if cohort:
        fields["cohort"]=cohort
        fields["cohort_evidence"]="page_text"
        fields["cohort_confirmed"]=True
    fields["source_card_text"]=raw[:500]
    return fields

def extract_boss_fields(raw_title, headings, text):
    """Read only publicly visible BOSS detail fields."""
    fields={}
    title_candidates=[clean(h) for h in headings if is_specific_role_title(h)]
    title=title_candidates[0] if title_candidates else ""
    raw=clean(raw_title)
    m=re.match(r"^(.{2,70}?)招聘[_｜|](.{2,70}?)(?:招聘信息|招聘)(?:[-_｜| ].*)?$",raw,re.I)
    if m:
        if not title: title=clean_job_title(m.group(1),"boss")
        fields["company"]=re.sub(r"招聘信息.*$","",m.group(2)).strip(" _-|｜")
    visible=(text or "")[:5000]
    if not fields.get("company"):
        m=re.search(r"(?:公司名称|企业名称|招聘企业|所属公司)\s*[:：]?\s*([^\s|｜]{2,60}(?:有限公司|集团|公司|大学|研究院|银行|科技|股份))",visible)
        if m: fields["company"]=m.group(1).strip()
    if not title:
        candidate=clean_job_title(raw,"boss")
        if is_specific_role_title(candidate): title=candidate
    if title: fields["title"]=title
    fields.update({k:v for k,v in extract_detail_fields_generic(text,title or raw).items() if v not in ("",None)})
    return fields

def extract_nowcoder_fields(raw_title, headings, text):
    """Parse a public Nowcoder detail page without folding card badges into the role name."""
    fields={}
    title=""
    company=""
    raw=clean(raw_title)
    for heading in headings:
        candidate=clean_job_title(heading,"nowcoder")
        if is_specific_role_title(candidate,"nowcoder"):
            title=candidate
            break
    m=re.match(r"^(.+?)_([^_]+?)(?:校招|实习|社招)_牛客网$",raw)
    if m:
        if not title: title=clean_job_title(m.group(1),"nowcoder")
        company=m.group(2).strip()
    visible=(text or "")[:5000]
    if not company:
        m=re.search(r"(?:公司名称|企业名称|招聘企业|公司信息|企业信息)\s*[:：]?\s*([^\s|｜]{2,60}(?:有限公司|集团|公司|大学|研究院|银行|证券|科技|股份))",visible)
        if m: company=m.group(1).strip()
    if not title:
        candidate=clean_job_title(raw,"nowcoder")
        if is_specific_role_title(candidate,"nowcoder"): title=candidate
    if title: fields["title"]=title
    if company: fields["company"]=company
    fields.update({k:v for k,v in extract_detail_fields_generic(text,title or raw).items() if v not in ("",None)})
    return fields

def merge_detail_fields(row, fields):
    for key,value in fields.items():
        if value not in (None,"",[],{}): row[key]=value
    return row

def record(company,title,url,source,cohort,confidence,program="",cohort_evidence=""):
    title=clean_job_title(title)
    direct=is_direct_detail_url(url)
    cohort_ok=bool(cohort and cohort.strip() not in ("待核验","未知") and cohort_evidence in COHORT_EVIDENCE_OK)
    title_ok=is_concrete_role_title(title)
    status="active" if confidence>=7 and direct and cohort_ok and title_ok else "pending_review"
    return {"id":make_id(company,title,url),"company":company,"title":title,
      "category":category(title),"city":"全国","enterprise_type":"待核验","degree":"待核验",
      "salary":"","publish_date":"","deadline":"","last_verified":"","last_collected":TODAY,"source":source,
      "source_url":url,"keywords":re.findall(r"[A-Za-z0-9+#.-]{2,}|[\u4e00-\u9fff]{2,8}",title)[:14],
      "status":status,"demo":False,"confirmed_by":[source],"cohort":cohort or "",
      "cohort_evidence":cohort_evidence or "","cohort_confirmed":bool(cohort_evidence),
      "program":program,"collector":"public-html-v5.5","granularity":"job" if status=="active" else "review",
      "verification_score":confidence,"description":"","responsibilities":"","requirements":"","benefits":"",
      "company_info":"","address":"","work_time":"","experience":"","job_type":"","headcount":""}

def embedded_records(company,source,parser,cohort,source_label=None,cohort_evidence=""):
    out=[]; host=urlparse(source).netloc.lower()
    # Only parse JSON embedded in public HTML; never call hidden APIs.
    for blob in parser.scripts:
        if not (blob.startswith("{") or blob.startswith("[")): continue
        try: obj=json.loads(blob)
        except Exception: continue
        stack=[obj]
        while stack and len(out)<500:
            node=stack.pop()
            if isinstance(node,dict):
                title=""
                employer_name=""
                for key in ("companyName","company_name","employerName","company"):
                    val=node.get(key)
                    if isinstance(val,str) and 1<len(val.strip())<90 and val.strip().lower() not in {"boss直聘","应届生求职网","牛客","秋招网"}:
                        employer_name=val.strip(); break
                for key in ("positionName","jobName","recruitJobName","postName","title","name"):
                    val=node.get(key)
                    if isinstance(val,str) and 3<len(val.strip())<90 and (is_specific_role_title(val) if source_label and "公开" in source_label else any(w.lower() in val.lower() for w in CONCRETE_WORDS)):
                        title=val.strip(); break
                url=""
                for key in ("positionUrl","jobUrl","detailUrl","url","href","link"):
                    val=node.get(key)
                    if isinstance(val,str) and val.startswith(("http://","https://","/")):
                        url=canonical(urljoin(source,val)); break
                if title and url and urlparse(url).netloc.lower()==host:
                    if source_label and "公开" in source_label and not is_direct_detail_url(url):
                        continue
                    sc=score(title,url,"")
                    if sc>=7 and any(x.lower() in url.lower() for x in ROLE_HREF):
                        item=record(employer_name or company,title,url,source_label or company+"官方招聘",cohort,sc,cohort_evidence=cohort_evidence)
                        if source_label=="应届生求职网公开岗位":
                            item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                        if source_label and "公开" in source_label: item["confirmed_by"]=[source_label]
                        out.append(item)
                stack.extend(v for v in node.values() if isinstance(v,(dict,list)))
            elif isinstance(node,list): stack.extend(v for v in node if isinstance(v,(dict,list)))
    return out

def table_records(company,source,parser,cohort,source_label=None,cohort_evidence=""):
    out=[]
    cities=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外","全国")
    degree_words=("博士","硕士","本科","大专")
    for row_index,row in enumerate(parser.tables):
        if len(row)<2: continue
        joined=" | ".join(row)
        if any(x in joined for x in NAV_BAD): continue
        # Keep actual role names, not category headings or copied qualification paragraphs.
        role_cells=row[1:] if source_label and "公开" in source_label else row
        role=next((x for x in role_cells if 2<len(x)<=45
                   and any(w.lower() in x.lower() for w in CONCRETE_WORDS)
                   and (not source_label or "公开" not in source_label or is_specific_role_title(x))
                   and not x.endswith(("类","方向","相关专业","等相关专业","专业"))
                   and not x.startswith(("具有","负责","熟悉","掌握","参与","岗位","要求","本科","硕士","博士"))),None)
        if not role: continue
        role=re.sub(r"\s*更新[:：]?\s*.*$","",role).strip()
        if not role: continue
        employer=company
        if source_label and "公开" in source_label:
            role_idx=next((i for i,x in enumerate(row) if x==role),-1)
            city_tokens=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外","全国","台北","新竹")
            bad_terms=("2027","2026","2025","校招","校园招聘","实习","社招","投递","截止","更新","登录","未公布","已结束","招聘对象","工作地点","届次批次","岗位","职位","薪资")
            candidates=[]
            for i,candidate in enumerate(row):
                candidate=clean(candidate)
                if i==role_idx or not (2<=len(candidate)<=60): continue
                if any(tok in candidate for tok in city_tokens+bad_terms) or any(tok in candidate for tok in degree_words): continue
                if candidate in NAV_BAD or candidate in PROGRAM_WORDS: continue
                if i>role_idx and any(w.lower() in candidate.lower() for w in CONCRETE_WORDS): continue
                if re.fullmatch(r"[\d年月日./:-]+",candidate): continue
                candidates.append(candidate)
            if candidates:
                employer=candidates[0]
                employer=re.split(r"\s+(?:金融|互联网/科技/AI|中国大陆|香港|美国|英国|新加坡|其他|金融/银行/证券|生产制造/工业|汽车/新能源|物流/供应链|快消/零售|生物医药/制药|教育/培训|房地产/建筑|咨询/四大|外企/合资|私企/民企|央国企/事业单位)\b",employer,1)[0].strip()
                employer=re.sub(r"(世界500强|知名互联网|独角兽|半导体大厂|头部外企|新势力车企|大模型公司|行业领先|上市公司|福利完善|成长空间大|团队氛围好).*$","",employer).strip(" ·-_")
            if not employer or employer==company: continue
        role_url=source
        row_links=parser.table_links[row_index] if row_index<len(getattr(parser,"table_links",[])) else []
        for href in row_links:
            full=urljoin(source,href)
            if urlparse(full).scheme in ("http","https") and any(x.lower() in full.lower() for x in ROLE_HREF):
                role_url=canonical(full); break
        if source_label and "公开" in source_label and not is_direct_detail_url(role_url):
            continue
        score_value=5 if source_label and "公开" in source_label else 8
        r=record(employer,role,role_url,source_label or company+"官方招聘",cohort,score_value,cohort_evidence=cohort_evidence)
        if source_label and "公开" in source_label:
            r["status"]="pending_review"; r["granularity"]="review"; r["verification_score"]=5
            r["confirmed_by"]=[source_label]
        found_cities=[]
        for city in cities:
            if city in joined and city not in found_cities: found_cities.append(city)
        if found_cities: r["city"]="、".join(found_cities)
        degree=next((x for x in row if any(d in x for d in degree_words) and len(x)<=35),None)
        if degree: r["degree"]=degree
        out.append(r)
    return out

def collect_source(src):
    company,root=src["company"],src["url"]; max_pages=int(src.get("max_pages",6)); max_candidates=int(src.get("max_candidates",500)); queue=list(src.get("seed_urls") or [root])[:max_pages]; visited=set(); seen=set(); rows=[]; qzw_detail_metadata={}
    diagnostics={"pages_attempted":0,"pages_failed":0,"html_bytes":0,"links_seen":0,"tables_seen":0,"embedded_json_blocks":0,"role_like_links":0,"discovery_pages_queued":0,"listing_pages_queued":0,"listing_url_candidates":[],"listing_urls_visited":[],"link_samples":[],"link_sample_urls":set(),"direct_detail_pages_visited":0,"direct_detail_links_queued":0,"direct_detail_links_skipped_access_limit":0,"direct_detail_requests_attempted":0,"detail_redirects":0,"detail_redirect_samples":[],"direct_detail_samples":[],"browser_render_attempts":0,"browser_render_successes":0,"browser_render_blocked":0,"browser_rendered_body_chars":0,"browser_rendered_links":0,"browser_render_samples":[],"sample_title":"","last_error":"","parser_modes":[]}
    queue.sort(key=lambda value:discovery_priority(src.get("id",""),value))
    while queue and len(visited)<max_pages and len(rows)<max_candidates:
        rows_before_page=len(rows)
        page=queue.pop(0)
        cp=(page.split("#",1)[0] if src.get("id")=="qiuzhaowang" else canonical(page))
        if cp in visited: continue
        if src.get("id")=="qiuzhaowang" and diagnostics["pages_attempted"]: time.sleep(0.5)
        elif src.get("id") in {"yingjiesheng","boss","nowcoder"} and diagnostics["pages_attempted"]: time.sleep(0.35)
        diagnostics["pages_attempted"]+=1
        if is_direct_detail_url(page):
            diagnostics["direct_detail_requests_attempted"]+=1
        try: html,final,fetch_meta=fetch(page,src.get("id",""))
        except Exception as exc:
            diagnostics["pages_failed"]+=1
            diagnostics["last_error"]=f"{type(exc).__name__}: {str(exc)[:140]}"
            continue
        visited.add(cp)
        if fetch_meta.get("browser_attempted"): diagnostics["browser_render_attempts"]+=1
        if fetch_meta.get("browser_success"): diagnostics["browser_render_successes"]+=1
        if fetch_meta.get("browser_blocked"): diagnostics["browser_render_blocked"]+=1
        diagnostics["browser_rendered_body_chars"]+=int(fetch_meta.get("browser_body_chars") or 0)
        diagnostics["browser_rendered_links"]+=int(fetch_meta.get("browser_links") or 0)
        if fetch_meta.get("browser_attempted") and len(diagnostics["browser_render_samples"])<8:
            diagnostics["browser_render_samples"].append({
                "requested_url":canonical(page),"mode":fetch_meta.get("mode","static"),
                "final_url":fetch_meta.get("browser_final_url",""),"title":fetch_meta.get("browser_title",""),
                "http_status":fetch_meta.get("browser_http_status",0),"body_chars":fetch_meta.get("browser_body_chars",0),
                "link_count":fetch_meta.get("browser_links",0),"body_preview":fetch_meta.get("browser_body_preview",""),
                "access_limited":bool(fetch_meta.get("browser_access_limited")),
                "error":fetch_meta.get("browser_error","")
            })
        if fetch_meta.get("browser_attempted") and not fetch_meta.get("browser_success") and fetch_meta.get("browser_error"):
            diagnostics["last_error"]="Browser-render fallback unavailable: "+str(fetch_meta["browser_error"])[:140]
        if fetch_meta.get("browser_blocked"):
            diagnostics["last_error"]="Public browser rendering reached a security/CAPTCHA wall or left the source allow-list; not bypassed"
            continue
        source_id=src.get("id","")
        if source_id in {"yingjiesheng","nowcoder","boss","51job","iguopin"} and should_discover_public_url(source_id,final) and not is_direct_detail_url(final):
            listing_url=canonical(final)
            if listing_url not in diagnostics["listing_urls_visited"] and len(diagnostics["listing_urls_visited"])<600:
                diagnostics["listing_urls_visited"].append(listing_url)
        diagnostics["html_bytes"]+=len(html.encode("utf-8",errors="ignore"))
        p=Parser(); p.feed(html)
        source_id=src.get("id","")
        if source_id in {"boss","51job","iguopin"}:
            for anchor in p.links:
                raw_href=anchor.get("href","")
                if not raw_href: continue
                link_url=canonical(urljoin(final,raw_href))
                title_text=clean(anchor.get("text",""))
                rank=diagnostic_link_priority(source_id,link_url,title_text)
                if rank>=9 or not link_url: continue
                if link_url in diagnostics["link_sample_urls"]: continue
                diagnostics["link_sample_urls"].add(link_url)
                diagnostics["link_samples"].append({
                    "_priority":rank,
                    "text":title_text[:100],
                    "url":link_url[:300],
                    "same_site":same_host(final,link_url),
                    "allowed":should_discover_public_url(source_id,link_url),
                    "direct_detail":is_direct_detail_url(link_url)
                })
            diagnostics["link_samples"].sort(key=lambda item:(item["_priority"],not item["direct_detail"],not item["allowed"],item["url"]))
            diagnostics["link_samples"]=diagnostics["link_samples"][:80]
            diagnostics["link_sample_urls"]={item["url"] for item in diagnostics["link_samples"]}
        page_text=p.page_text()
        raw_page_title=clean(p.page_title)
        if not diagnostics["sample_title"]: diagnostics["sample_title"]=raw_page_title[:140]
        diagnostics["links_seen"]+=len(p.links); diagnostics["tables_seen"]+=len(p.tables); diagnostics["embedded_json_blocks"]+=len(p.scripts)
        detail_heading=""
        if is_direct_detail_url(page) and not is_direct_detail_url(final):
            diagnostics["detail_redirects"]+=1
            if len(diagnostics["detail_redirect_samples"])<4:
                diagnostics["detail_redirect_samples"].append({
                    "requested_path":urlparse(page).path,
                    "final_host":urlparse(final).netloc.lower(),
                    "final_path":urlparse(final).path,
                    "page_title":raw_page_title[:120]
                })
        if is_direct_detail_url(final):
            diagnostics["direct_detail_pages_visited"]+=1
            if len(diagnostics["direct_detail_samples"])<4:
                diagnostics["direct_detail_samples"].append({
                    "url_path":urlparse(final).path,
                    "page_title":raw_page_title[:160],
                    "text_preview":page_text[:600]
                })
            detail_heading=next((clean_job_title(head,src.get("id","")) for head in p.headings
                                 if (is_specific_role_title(clean_job_title(head,src.get("id","")),src.get("id",""))
                                     if src.get("id") in {"nowcoder","yingjiesheng","boss","qiuzhaowang","iguopin","51job"}
                                     else is_concrete_role_title(clean_job_title(head,src.get("id","")),src.get("id","")))), "")
        explicit_cohort=year(raw_page_title+" "+page_text+" "+" ".join(p.headings))
        source_cohort=src.get("cohort","") if src.get("type")=="official_campus" else ""
        if src.get("id")=="qiuzhaowang" and re.search(r"(?:[?&])year=2027(?:&|$)",page):
            source_cohort="2027届"
        cohort=explicit_cohort or source_cohort
        cohort_evidence=("page_text" if explicit_cohort else "")
        page_company=company
        qzw_meta=qzw_detail_metadata.get(canonical(final),{}) if src.get("id")=="qiuzhaowang" else {}
        if qzw_meta.get("company"): page_company=qzw_meta["company"]
        qzw_role_hint=qzw_meta.get("title","")
        detail_fields={}
        if src.get("id")=="yingjiesheng" and is_direct_detail_url(final):
            detail_fields=extract_yingjiesheng_fields(raw_page_title,page_text,final)
            if detail_fields.get("title"): raw_page_title=detail_fields["title"]
            if detail_fields.get("company"): page_company=detail_fields["company"]
        elif src.get("id")=="nowcoder" and is_direct_detail_url(final):
            detail_fields=extract_nowcoder_fields(raw_page_title,p.headings,page_text)
            if detail_fields.get("title"): raw_page_title=detail_fields["title"]
            if detail_fields.get("company"): page_company=detail_fields["company"]
        elif src.get("id")=="boss" and is_direct_detail_url(final):
            detail_fields=extract_boss_fields(raw_page_title,p.headings,page_text)
            if detail_fields.get("title"): raw_page_title=detail_fields["title"]
            if detail_fields.get("company"): page_company=detail_fields["company"]
        record_source=company+"官方招聘"
        if src.get("id")=="nowcoder":
            # The company must come from a visible detail title/field, not the source-site label.
            m=re.search(r"_([^_]+?)(?:校招|实习|社招)_牛客网", raw_page_title)
            if m: page_company=m.group(1).strip()
            if detail_fields.get("company"): page_company=detail_fields["company"]
            record_source="牛客公开岗位"
        elif src.get("id")=="yingjiesheng":
            # Company/title were extracted from visible fields above where available.
            record_source="应届生求职网公开岗位"
        elif src.get("id")=="boss":
            record_source="BOSS直聘公开校招岗位"
        elif src.get("id")=="iguopin":
            record_source="国聘公开招聘"
        elif src.get("id")=="51job":
            record_source="前程无忧校园招聘公开岗位"
        elif src.get("id")=="qiuzhaowang":
            parts=re.split(r"[|｜]",raw_page_title)
            if qzw_meta.get("company"):
                page_company=qzw_meta["company"]
            elif "/jobs/" in urlparse(final).path and parts and not re.search(r"(?:最新更新|招聘信息|秋招网|校招岗位)",parts[0]):
                page_company=parts[0].strip()
            record_source="秋招网公开聚合"
        elif src.get("id")=="mianlingai":
            record_source="面灵AI公开聚合"
        elif src.get("id")=="91bangtu":
            record_source="91邦途公开聚合"
        page_title=clean_job_title(detail_fields.get("title") or detail_heading or raw_page_title,src.get("id",""))
        detail_role_titles=list(detail_fields.get("role_titles") or []) if src.get("id")=="yingjiesheng" else []
        detail_fields_for_merge={k:v for k,v in detail_fields.items() if k not in {"title","role_titles","company"}}
        if src.get("id")=="nowcoder":
            page_title=clean_job_title(detail_heading or p.page_title,"nowcoder")
        if is_direct_detail_url(final):
            for k,v in extract_detail_fields_generic(page_text,detail_heading or raw_page_title).items():
                detail_fields.setdefault(k,v)
        if src.get("id")=="qiuzhaowang" and urlparse(final).path=="/latest":
            seen_groups=set()
            role_hints=("工程师","研发","算法","产品","经理","销售","财务","设计","运营","管理","技术","教师","审计","法律","采购","管培","分析","工艺","自动化","质量","研究","岗位","方向","PM","AI","硬件","软件","芯片","医学","金融","会计","营销","供应链","商务","专员","服务","项目","研究员","分析师","咨询","物流","顾问","工程技术")
            for link_index,a in enumerate(p.links):
                group_title=clean(a.get("text",""))
                group_href=urljoin(final,a.get("href",""))
                if not same_host(final,group_href) or "/jobs/" not in urlparse(group_href).path: continue
                if len(group_title)<3 or len(group_title)>500: continue
                if group_title.startswith(("查看","开通会员","登录")) or group_title in ("岗位发现","我的机会","求职记录","全部岗位"): continue
                if any(x.lower() in group_title.lower() for x in NAV_BAD): continue
                if not any(x.lower() in group_title.lower() for x in role_hints): continue
                group_url=canonical(group_href)
                if not is_direct_detail_url(group_url) or group_url in seen_groups: continue
                seen_groups.add(group_url)
                previous=[]
                for prev in reversed(p.links[max(0,link_index-4):link_index]):
                    candidate=clean(prev.get("text",""))
                    candidate_url=urljoin(final,prev.get("href",""))
                    if not (2<=len(candidate)<=70) or "/jobs/" in urlparse(candidate_url).path: continue
                    if candidate in ("秋招网","岗位发现","我的机会","求职记录","校招资料","求职指南","会员权益"): continue
                    if any(x.lower() in candidate.lower() for x in NAV_BAD) or is_specific_role_title(candidate,"qiuzhaowang"): continue
                    if re.fullmatch(r"[\d年月日./:：-]+",candidate): continue
                    previous.append(candidate)
                company_name=next((x for x in previous if any(mark in x for mark in ("有限公司","股份","集团","公司","科技","银行","证券","研究院","大学","医院","中心"))),"")
                if not company_name and previous: company_name=previous[0]
                hint=re.sub(r"\s*(?:工作地点|工作城市|面向届次|届次批次|招聘项目包含).*?$","",group_title).strip()
                qzw_detail_metadata[group_url]={"company":company_name or company,"title":hint}
                if group_url not in visited and group_url not in queue and len(queue)<max_pages:
                    queue.append(group_url)
                    diagnostics["discovery_pages_queued"]+=1
                    diagnostics["direct_detail_links_queued"]+=1
        if src.get("id")=="qiuzhaowang" and is_direct_detail_url(final) and "/jobs/" in urlparse(final).path:
            qzw_roles=extract_qiuzhaowang_role_names(page_text,qzw_role_hint)
            if not qzw_roles:
                for heading in p.headings:
                    candidate=clean_job_title(heading,"qiuzhaowang")
                    if is_specific_role_title(candidate,"qiuzhaowang") and candidate not in qzw_roles:
                        qzw_roles.append(candidate)
            city_match=re.search(r"工作城市\s*(.+?)\s*面向届次",page_text)
            city_text=clean(city_match.group(1)) if city_match else ""
            for role_name in qzw_roles:
                item=record(page_company,role_name,final,record_source,cohort,5,cohort_evidence=cohort_evidence)
                merge_detail_fields(item,detail_fields)
                item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                if city_text: item["city"]=city_text
                item["confirmed_by"]=[record_source]
                item["last_collected"]=TODAY; item["last_verified"]=TODAY; item["last_verified_at"]=STAMP
                item["verification_status"]="link_alive"; item["verification_http_status"]=200
                item["verification_note"]="公开岗位详情页可读取；届别与有效期仍需独立核验"
                rows.append(item)
        candidate_page_titles=detail_role_titles or ([page_title] if page_title else [])
        for candidate_page_title in candidate_page_titles:
            candidate_page_title=clean_job_title(candidate_page_title,src.get("id",""))
            page_title_ok=is_specific_role_title(candidate_page_title,src.get("id","")) if "公开" in record_source else is_concrete_role_title(candidate_page_title,src.get("id",""))
            if src.get("id")=="boss" and not (detail_fields.get("company") or (page_company and page_company not in {company,"BOSS直聘"})):
                page_title_ok=False
            if not candidate_page_title or not page_title_ok:
                continue
            sc=score(candidate_page_title,final,page_text)
            if sc>=5 and is_direct_detail_url(final):
                item=record(page_company,candidate_page_title,final,record_source,cohort,sc,cohort_evidence=cohort_evidence)
                merge_detail_fields(item,detail_fields_for_merge)
                item["cohort"]=item.get("cohort") or cohort or ""
                item["cohort_evidence"]=item.get("cohort_evidence") or cohort_evidence or ""
                item["cohort_confirmed"]=bool(item.get("cohort_confirmed") or item["cohort_evidence"])
                if item.get("deadline") and item["deadline"] < TODAY:
                    item["status"]="expired"; item["granularity"]="review"; item["verification_score"]=min(item.get("verification_score",5),4)
                if "已结束" in page_text[:1000]:
                    item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=min(item.get("verification_score",5),4)
                item["last_collected"]=TODAY
                if is_direct_detail_url(final):
                    item["last_verified"]=TODAY; item["last_verified_at"]=STAMP
                    item["verification_status"]="link_alive"; item["verification_http_status"]=200
                    item["verification_note"]="采集器成功读取公开详情页；不代表雇主再次确认仍在招聘"
                rows.append(item)
        if src.get("id")!="qiuzhaowang":
            table_rows=table_records(page_company,final,p,cohort,record_source,cohort_evidence)
            embedded_rows=embedded_records(page_company,final,p,cohort,record_source,cohort_evidence)
            parsed_rows=table_rows+embedded_rows
            if "公开" in record_source:
                # Embedded/listing data only yields discovery URLs. Publish a record only
                # after that URL's own detail page has been fetched and parsed.
                scheduled_now={canonical(value) for value in queue}
                for candidate in parsed_rows:
                    candidate_url=canonical(candidate.get("source_url",""))
                    if not candidate_url or candidate_url==canonical(final) or candidate_url in visited or candidate_url in scheduled_now:
                        continue
                    if not is_direct_detail_url(candidate_url) or len(queue)>=max_pages:
                        continue
                    queue.append(candidate_url); scheduled_now.add(candidate_url)
                    diagnostics["discovery_pages_queued"]+=1
                    diagnostics["direct_detail_links_queued"]+=1
            else:
                for extracted in parsed_rows:
                    if detail_fields: merge_detail_fields(extracted,detail_fields)
                    extracted["last_collected"]=TODAY; extracted["last_verified"]=""
                    extracted["cohort_evidence"]=extracted.get("cohort_evidence") or cohort_evidence or ""
                    extracted["cohort_confirmed"]=bool(extracted.get("cohort_confirmed") or extracted["cohort_evidence"])
                    if not is_direct_detail_url(final) or extracted.get("source_url","")!=final or not extracted["cohort_confirmed"]:
                        extracted["status"]="pending_review"; extracted["granularity"]="review"; extracted["verification_score"]=min(extracted.get("verification_score",5),5)
                rows.extend(parsed_rows)
        # Discover public category/list/pagination pages separately from role-title links.
        listing_limit=int(src.get("listing_page_limit",max(6,max_pages//2)))
        scheduled={canonical(value) for value in queue}
        for link in p.links:
            discovery_url=canonical(urljoin(final,link.get("href","")))
            if not discovery_url or discovery_url in visited or discovery_url in scheduled: continue
            if not should_discover_public_url(src.get("id",""),discovery_url): continue
            is_detail=is_direct_detail_url(discovery_url)
            if source_id=="boss" and is_detail:
                # BOSS vacancy links consistently redirect to a security interstitial;
                # retain the card lead, but don't spend the page budget retrying detail URLs.
                diagnostics["direct_detail_links_skipped_access_limit"]+=1
                continue
            if (not is_detail) and src.get("id") in {"yingjiesheng","nowcoder","boss","51job","iguopin"}:
                candidates=diagnostics["listing_url_candidates"]
                if discovery_url not in candidates and len(candidates)<1200:
                    candidates.append(discovery_url)
            if not is_detail and diagnostics["listing_pages_queued"]>=listing_limit: continue
            if len(queue)>=max_pages: continue
            queue.append(discovery_url); scheduled.add(discovery_url)
            diagnostics["discovery_pages_queued"]+=1
            if not is_detail: diagnostics["listing_pages_queued"]+=1
        queue.sort(key=lambda value:discovery_priority(src.get("id",""),value))
        for link_index,a in enumerate(p.links):
            title=clean_job_title(a["text"],src.get("id","")); href=canonical(urljoin(final,a["href"]))
            if not title or len(title)<3 or len(title)>100 or not href or href in seen: continue
            title_ok=is_specific_role_title(title) if "公开" in record_source else is_concrete_role_title(title)
            if not title_ok: continue
            if not same_host(final,href): continue
            if "公开" in record_source:
                # BOSS role cards may be retained only from an allowed public campus listing page.
                # Never reinterpret general consumer search results as campus recruitment.
                if src.get("id")=="boss" and (not should_discover_public_url("boss",final) or is_direct_detail_url(final)):
                    continue
                # Public BOSS job cards can be leads if their real detail URL exists and
                # company/title are visible; the detail fetch itself may still be blocked.
                if src.get("id")=="boss" and is_direct_detail_url(href):
                    card=extract_boss_card_fields(a["text"],p.links,link_index,final)
                    if card:
                        company_name=card.pop("company")
                        clean_title=card.pop("title")
                        item=record(company_name,clean_title,href,record_source,card.get("cohort",""),5,
                                    cohort_evidence=card.get("cohort_evidence",""))
                        merge_detail_fields(item,card)
                        item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                        item["verification_status"]="unknown"
                        item["verification_note"]="BOSS公开校招卡片可见且有岗位级详情链接；详情访问可能受平台安全验证限制，本条未标记为核验成功"
                        item["last_collected"]=TODAY; item["last_verified"]=""
                        item["confirmed_by"]=[record_source]
                        rows.append(item)
                        seen.add(href)
                    # Do not enqueue the BOSS detail URL: the public list card is kept as a
                    # pending lead, and the verifier will not treat a security redirect as alive.
                # Other aggregators must have a parseable direct detail page before we retain them.
                elif is_direct_detail_url(href) and href not in visited and href not in queue and len(queue)<max_pages:
                    queue.append(href)
                    diagnostics["discovery_pages_queued"]+=1
                    diagnostics["direct_detail_links_queued"]+=1
                continue
            if any(x.lower() in title.lower() for x in NAV_BAD): continue
            # Generic corporate pages such as financial-market pages must not
            # become false job leads merely because their titles contain "市场".
            if not any(x.lower() in href.lower() for x in ROLE_HREF): continue
            diagnostics["role_like_links"]+=1
            sc=score(title,href,page_text)
            if sc<5: continue
            seen.add(href)
            if src.get("id")=="qiuzhaowang": continue
            link_record=record(page_company,title,href,record_source,cohort,sc,cohort_evidence=cohort_evidence)
            # List discoveries remain leads until the direct detail page is fetched.
            link_record["status"]="pending_review"; link_record["granularity"]="review"; link_record["verification_score"]=min(link_record.get("verification_score",sc),5)
            link_record["last_collected"]=TODAY; link_record["last_verified"]=""
            if detail_fields: merge_detail_fields(link_record,detail_fields)
            if "公开岗位" in record_source or "公开聚合" in record_source:
                link_record["confirmed_by"]=[record_source]
            rows.append(link_record)
            if len(queue)<max_pages and href not in visited and href not in queue and any(x.lower() in href.lower() for x in ROLE_HREF):
                queue.append(href)
        # If a public official page exposes a 2027 recruitment announcement but no job-level
        # data, retain exactly one clearly-labelled lead instead of mislabelling it as a job.
        # We do not synthesize salary, location, degree, or job descriptions for this lead.
        if len(rows)==rows_before_page and not is_direct_detail_url(final):
            announcement_candidates=[]
            for link in p.links:
                ann_title=clean(link.get("text",""))
                if len(ann_title)<8 or len(ann_title)>180: continue
                if not re.search(r"(?:20\d{2}\s*届|20\d{2}年|27届)",ann_title): continue
                if not any(term in ann_title for term in PROGRAM_WORDS): continue
                if any(term.lower() in ann_title.lower() for term in NAV_BAD): continue
                ann_url=canonical(urljoin(final,link.get("href","")))
                if not ann_url or not same_host(final,ann_url): continue
                announcement_candidates.append((ann_title,ann_url))
            if not announcement_candidates and re.search(r"(?:20\d{2}\s*届|20\d{2}年|27届)",raw_page_title) and any(term in raw_page_title for term in PROGRAM_WORDS):
                announcement_candidates.append((raw_page_title,canonical(final)))
            if not announcement_candidates:
                # Some public campus portals render job cards via client-side JS but expose
                # an explicit 2027 cohort in their visible banner/alt text. Keep one landing-page
                # lead only; never interpret those banners as individual vacancies.
                visible_year=year(page_text)
                ann_terms=("校园招聘","招聘公告","招聘计划","应届生招聘","校招正式批","校招公告","秋季校园招聘")
                if visible_year and any(term in page_text for term in ann_terms):
                    announcement_candidates.append((f"{company} {visible_year}校园招聘入口线索",canonical(final)))
            portal_candidate=False
            if not announcement_candidates and src.get("type")=="official_campus" and source_cohort:
                title_year=year(raw_page_title)
                source_context=(final+" "+raw_page_title+" "+src.get("url","")).lower()
                campus_hints=("campus","校招","校园招聘","招聘项目","招聘公告","职位列表","招聘网站","search.html","/jobs","/job/")
                if (not title_year or title_year==source_cohort) and any(hint in source_context for hint in campus_hints) and not is_direct_detail_url(final):
                    announcement_candidates.append((f"{company} {source_cohort}官方招聘入口线索",canonical(final)))
                    portal_candidate=True
            if announcement_candidates:
                ann_title,ann_url=max(announcement_candidates,key=lambda item:(len(item[0]),item[0]))
                ann_year=year(ann_title) or cohort or source_cohort
                ann=record(page_company,ann_title,ann_url,record_source,ann_year,2,
                           program=("官方招聘入口线索" if portal_candidate else "招聘公告/计划线索"),
                           cohort_evidence=("source_registry" if portal_candidate else "visible_announcement_title"))
                ann["status"]="pending_review"
                ann["granularity"]="entry" if portal_candidate else "announcement"
                ann["verification_score"]=2
                ann["last_verified"]=""
                ann["last_collected"]=TODAY
                ann["announcement_only"]=not portal_candidate
                ann["entry_only"]=portal_candidate
                rows.append(ann)
    # de-dupe within source, preferring higher confidence
    best={}
    for r in rows:
        k=(r["company"],r["title"],r["source_url"])
        if k not in best or r["verification_score"]>best[k]["verification_score"]: best[k]=r
    best_rows=list(best.values())
    role_rows=[r for r in best_rows if r.get("granularity") not in {"announcement","entry"}]
    announcement_rows=[r for r in best_rows if r.get("granularity")=="announcement"]
    entry_rows=[r for r in best_rows if r.get("granularity")=="entry"]
    diagnostics["role_records"]=len(role_rows)
    diagnostics["announcement_records"]=len(announcement_rows)
    diagnostics["portal_entries"]=len(entry_rows)
    if src.get("id")=="boss" and role_rows and diagnostics["direct_detail_links_skipped_access_limit"]>0 and diagnostics["direct_detail_pages_visited"]==0:
        diagnostics["source_status"]="public_card_leads_only"
    else:
        diagnostics["source_status"]=("ok" if role_rows else
            ("detail_access_restricted" if diagnostics.get("direct_detail_requests_attempted",0)>0 and diagnostics.get("direct_detail_pages_visited",0)==0 and diagnostics.get("detail_redirects",0)>0 else
             ("announcement_only" if announcement_rows else
              ("entry_only" if entry_rows else
               ("listing_only_no_job_details" if diagnostics["listing_pages_queued"] and visited else
                ("reachable_no_records" if visited else "access_error"))))))
    diagnostics["parser_modes"]=[name for name,count in (("links",diagnostics["links_seen"]),("tables",diagnostics["tables_seen"]),("embedded_json",diagnostics["embedded_json_blocks"])) if count]
    return best_rows,visited,diagnostics

def main():
    sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
    STATE_PATH=ROOT/"data/collection-state.json"
    try:
        state=json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state={"qiuzhaowang_next_page":21}
    state.setdefault("source_listing_url_pool",{})
    state.setdefault("source_listing_cursor",{})
    rotating_sources={"yingjiesheng","nowcoder","boss","51job","iguopin"}
    rotation_meta={}
    for source in sources:
        source_id=source.get("id","")
        if source_id not in rotating_sources: continue
        base_seeds=[normalize_source_url(source_id,u) for u in (source.get("seed_urls") or [source.get("url","")])]
        base_set={normalize_source_url(source_id,u) for u in base_seeds if u}
        stored_pool=[]
        for url in state["source_listing_url_pool"].get(source_id,[]):
            normalized=normalize_source_url(source_id,str(url))
            if normalized and normalized not in base_set and not is_direct_detail_url(normalized) and should_discover_public_url(source_id,normalized) and normalized not in stored_pool:
                stored_pool.append(normalized)
        cursor=int(state["source_listing_cursor"].get(source_id,0))
        batch_size=int(source.get("rotation_seed_batch",12))
        pool_limit=int(source.get("listing_pool_limit",600))
        batch,next_cursor=select_rotation_batch(stored_pool,cursor,batch_size)
        source["seed_urls"]=list(dict.fromkeys(base_seeds+batch))[:int(source.get("max_pages",80))]
        rotation_meta[source_id]={"base_seeds":base_seeds,"pool":stored_pool,"cursor":cursor,"batch":batch,"next_cursor":next_cursor,"batch_size":batch_size,"pool_limit":pool_limit}
    rotation_start=int(state.get("qiuzhaowang_next_page",21))
    rotation_pages=[]
    page_num=rotation_start
    while len(rotation_pages)<10:
        if page_num>192: page_num=11
        if page_num not in range(1,11) and page_num not in rotation_pages:
            rotation_pages.append(page_num)
        page_num+=1
    qiuzhaowang_pages=list(range(1,11))+rotation_pages
    for source in sources:
        if source.get("id")=="qiuzhaowang":
            source["seed_urls"]=[f"https://qiuzhaowang.com/latest?page={n}&year=2027" for n in qiuzhaowang_pages]
            source["max_pages"]=50
            source["max_candidates"]=6000
    enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
    all_rows=[]; results=[]; active=review=0; qiuzhaowang_visited=set()
    with ThreadPoolExecutor(max_workers=min(12,len(enabled) or 1)) as pool:
        futures={pool.submit(collect_source,s):s for s in enabled}
        for fut in as_completed(futures):
            s=futures[fut]
            try: rows,visited,diag=fut.result()
            except Exception as exc:
                rows,visited=[],set()
                diag={"pages_attempted":1,"pages_failed":1,"html_bytes":0,"links_seen":0,"tables_seen":0,
                      "embedded_json_blocks":0,"role_like_links":0,"sample_title":"","last_error":f"{type(exc).__name__}: {str(exc)[:140]}",
                      "parser_modes":[],"source_status":"access_error"}
            if s.get("id")=="qiuzhaowang": qiuzhaowang_visited=visited
            source_id=s.get("id","")
            if source_id in rotation_meta:
                meta=rotation_meta[source_id]
                base_set={canonical(u) for u in meta["base_seeds"] if u}
                pool=list(meta["pool"])
                for url in diag.get("listing_url_candidates",[]):
                    normalized=normalize_source_url(source_id,str(url))
                    if (normalized and normalized not in base_set and not is_direct_detail_url(normalized)
                            and should_discover_public_url(source_id,normalized) and normalized not in pool):
                        pool.append(normalized)
                state["source_listing_url_pool"][source_id]=pool[-meta["pool_limit"]:]
                if not meta["pool"] and not meta["batch"]:
                    visited_listing={canonical(u) for u in diag.get("listing_urls_visited",[])}
                    positions=[i for i,url in enumerate(pool) if url in visited_listing]
                    cursor=max(positions)+1 if positions else 0
                else:
                    cursor=meta["next_cursor"]
                state["source_listing_cursor"][source_id]=cursor
                diag["listing_pool_size"]=len(state["source_listing_url_pool"][source_id])
                diag["listing_urls_visited_count"]=len(diag.get("listing_urls_visited",[]))
            all_rows.extend(rows)
            a=sum(r["status"]=="active" for r in rows); rr=sum(r["status"]!="active" for r in rows)
            active+=a; review+=rr
            results.append({"id":s["id"],"company":s["company"],"pages_visited":len(visited),"records_found":len(rows),
                            "active_candidates":a,"pending_review":rr,
                            "pages_attempted":diag.get("pages_attempted",0),"pages_failed":diag.get("pages_failed",0),
                            "html_bytes":diag.get("html_bytes",0),"links_seen":diag.get("links_seen",0),
                            "listing_url_candidates":len(diag.get("listing_url_candidates",[])),
                            "listing_urls_visited":diag.get("listing_urls_visited_count",len(diag.get("listing_urls_visited",[]))),
                            "listing_pool_size":diag.get("listing_pool_size",0),
                            "listing_cursor_next":state.get("source_listing_cursor",{}).get(s.get("id",""),0),
                            "tables_seen":diag.get("tables_seen",0),"embedded_json_blocks":diag.get("embedded_json_blocks",0),
                            "role_like_links":diag.get("role_like_links",0),"discovery_pages_queued":diag.get("discovery_pages_queued",0),
                            "listing_pages_queued":diag.get("listing_pages_queued",0),"direct_detail_pages_visited":diag.get("direct_detail_pages_visited",0),"direct_detail_links_queued":diag.get("direct_detail_links_queued",0),"direct_detail_links_skipped_access_limit":diag.get("direct_detail_links_skipped_access_limit",0),"direct_detail_requests_attempted":diag.get("direct_detail_requests_attempted",0),"detail_redirects":diag.get("detail_redirects",0),"detail_redirect_samples":diag.get("detail_redirect_samples",[]),"direct_detail_samples":diag.get("direct_detail_samples",[]),"browser_render_attempts":diag.get("browser_render_attempts",0),"browser_render_successes":diag.get("browser_render_successes",0),
                            "browser_render_blocked":diag.get("browser_render_blocked",0),"browser_rendered_body_chars":diag.get("browser_rendered_body_chars",0),
                            "browser_rendered_links":diag.get("browser_rendered_links",0),"browser_render_samples":diag.get("browser_render_samples",[]),
                            "role_records":diag.get("role_records",0),
                            "announcement_records":diag.get("announcement_records",0),"portal_entries":diag.get("portal_entries",0),
                            "sample_title":diag.get("sample_title",""),
                            "role_title_samples":[r.get("title","") for r in rows if r.get("granularity") not in {"announcement","entry"}][:8],
                            "role_field_samples":[{"title":r.get("title",""),"company":r.get("company",""),"city":r.get("city",""),"salary":r.get("salary",""),"degree":r.get("degree",""),"source_url":r.get("source_url","")} for r in rows if r.get("granularity") not in {"announcement","entry"}][:5],
                            "link_samples":[{k:v for k,v in item.items() if k!="_priority"} for item in diag.get("link_samples",[])],
                            "last_error":diag.get("last_error",""),"parser_modes":diag.get("parser_modes",[]),
                            "source_status":diag.get("source_status","access_error")})
    results.sort(key=lambda x:x["company"])
    rot_seen=set()
    for visited_url in qiuzhaowang_visited:
        match=re.search(r"[?&]page=(\d+)",visited_url)
        if match and int(match.group(1)) in rotation_pages: rot_seen.add(int(match.group(1)))
    next_page=rotation_start
    while next_page in rot_seen:
        next_page+=1
        if next_page>192: next_page=11
    state["qiuzhaowang_next_page"]=next_page
    state["qiuzhaowang_last_pages"]=qiuzhaowang_pages
    STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    INBOX.write_text(json.dumps(all_rows,ensure_ascii=False,indent=2),encoding="utf-8")
    MANIFEST.write_text(json.dumps({"run_date":TODAY,"collector_version":"5.5","enabled_sources":len(enabled),
     "pagination_state":{"qiuzhaowang_pages":qiuzhaowang_pages,"qiuzhaowang_next_page":next_page,
                         "source_listing_rotation":{sid:{"known_public_list_urls":len(state["source_listing_url_pool"].get(sid,[])),
                                                         "next_offset":state["source_listing_cursor"].get(sid,0),
                                                         "batch_size":rotation_meta[sid]["batch_size"]}
                                                     for sid in rotation_meta}},
     "records_found":len(all_rows),"active_candidates":active,"pending_review":review,"results":results,
     "policy":"Public official pages only; no login/CAPTCHA/private API/anti-bot bypass.",
     "granularity":"Only high-confidence concrete role records are active; program/announcement/navigation pages remain pending_review."},
     ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"sources={len(enabled)} records={len(all_rows)} active_candidates={active} pending_review={review}")
    

if __name__ == "__main__":
    main()
