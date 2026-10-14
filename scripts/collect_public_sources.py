"""V5.21 public recruitment collector.

Rules:
- public official pages only; no login/CAPTCHA/private API/anti-bot bypass.
- prefer concrete job-level listings.
- program/announcement pages are never promoted to active jobs.
- extract visible links, HTML tables/lists, JSON-LD and common embedded JSON.
- score records and only promote high-confidence concrete roles.
"""
import time
import os,json,re,hashlib,atexit,subprocess,threading
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
        super().__init__(); self.links=[]; self.text=[]; self.tables=[]; self.table_links=[]; self._row_links=[]; self._in_row=False; self.scripts=[]; self.page_title=""; self._in_title=False; self._script_buf=None; self._a=None; self._td=None; self._row=[]; self.headings=[]; self._heading_buf=None; self._heading_tag=None; self._style_depth=0; self._noscript_depth=0; self._template_depth=0
    def handle_starttag(self,tag,attrs):
        d=dict(attrs); tag=tag.lower()
        if tag=="style": self._style_depth+=1
        if tag=="noscript": self._noscript_depth+=1
        if tag=="template": self._template_depth+=1
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
        visible=(self._script_buf is None and not self._style_depth and not self._noscript_depth and not self._template_depth)
        if s and visible: self.text.append(s)
        if visible:
            if self._a is not None: self._a["text"]+=(" "+s if s else "")
            if self._in_title: self.page_title+=data
            if self._heading_buf is not None: self._heading_buf.append(data)
            if self._td is not None: self._td+=(" "+s if s else "")
        if self._script_buf is not None: self._script_buf.append(data)
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
        if tag=="style": self._style_depth=max(0,self._style_depth-1)
        if tag=="noscript": self._noscript_depth=max(0,self._noscript_depth-1)
        if tag=="template": self._template_depth=max(0,self._template_depth-1)
    def page_text(self): return " ".join(self.text)

_BROWSER_WORKER=None
_BROWSER_WORKER_LOCK=threading.Lock()

def should_render_public_page(source_id,url,html):
    """Render only anonymous public list pages; BOSS root lists may expose public category tabs."""
    if source_id=="iguopin":
        return should_discover_public_url(source_id,url) and not is_direct_detail_url(url)
    if source_id=="boss":
        if not should_discover_public_url(source_id,url) or is_direct_detail_url(url):
            return False
        path=urlparse(url).path.lower().rstrip("/")
        if path in {"/school","/xiaoyuan"}:
            return True
        parsed=Parser()
        parsed.feed(html or "")
        resolved=[canonical(urljoin(url,link.get("href",""))) for link in parsed.links if link.get("href")]
        return path.startswith(("/school/","/xiaoyuan/")) and not any(is_direct_detail_url(link) for link in resolved)
    if source_id=="51job":
        # Match the renderer's host/path boundary before asking it to render.
        # This avoids noisy "outside allow-list" failures for discovered off-host URLs.
        if not should_discover_public_url(source_id,url) or is_direct_detail_url(url):
            return False
        parsed_url=urlparse(url)
        host=parsed_url.hostname.lower() if parsed_url.hostname else ""
        path=parsed_url.path.lower()
        if not (host=="51job.com" or host.endswith(".51job.com")):
            return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path,re.I):
            return False
        if re.search(r"/(?:login|register|resume|member|account|passport|captcha)(?:/|$)",path,re.I):
            return False
        # Keep eligibility identical to public_page_renderer.cjs. The source
        # registry may discover a campus microsite root such as /asiainfo, but
        # the browser worker intentionally renders only a root, a real HTML/PHP
        # page, or a URL whose path/query clearly denotes recruitment navigation.
        # Reject it here so we don't emit a false "outside allow-list" error.
        public_route = (
            path == "/"
            or path.endswith((".html", ".php"))
            or bool(re.search(
                r"(?:jobs?|jobdetail|job-detail|position|positions|campus|company|page|pageno|jobid|positionid)",
                path + parsed_url.query, re.I
            ))
        )
        if not public_route:
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
    """Strip serialized card JSON / legacy CSS pollution from visible job text safely."""
    if not isinstance(value, str):
        return value
    text=value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
    text=text.replace("\\u002F", "/").replace("\\u002f", "/")
    text=re.sub(r"[ \t]+", " ", text).strip()

    # Older Yingjiesheng pages accidentally exposed legacy IE CSS rules as visible text.
    for _ in range(30):
        cleaned=re.sub(r"(?:^|\s)\.ie(?:11|undefined)\b[^{}]*\{[^{}]*\}", " ", text, flags=re.I)
        if cleaned==text:
            break
        text=cleaned
    text=re.sub(r"[ \t]{2,}", " ", text).strip()

    # Some YJS pages place job-search buttons (e.g. 模拟面试) before the real body.
    # Strip leading page chrome only when a substantive job-section heading follows it.
    section_heading=re.search(r"(?:职位描述|招聘岗位|岗位职责|工作职责|工作内容|岗位要求|任职要求|任职资格)\s*[:：]?",text,re.I)
    if section_heading and section_heading.start()>=24:
        prefix=text[:section_heading.start()]
        if re.search(r"(?:模拟面试|职位诱惑|岗位亮点|薪酬福利|工作地点|学历要求|招聘信息|发布时间|职位类型|职位类别|需求专业|公司信息|申请职位)",prefix,re.I):
            text=text[section_heading.start():].lstrip(" :-—")

    # Remove contact/application/company/navigation tails, but not buttons that appear above the body.
    tail_noise_pattern=r"\s*(?:联系方式|联系人|联系电话|联系邮箱|邮件联系|简历接收邮箱|简历投递|投递方式|投递邮箱|查看其他岗位|查看更多职位|投递说明|招聘流程|公司介绍|公司简介|上一条|下一条|上一篇|下一篇|上一页|下一页|返回顶部|登录/注册|收藏职位|职位收藏|信息来源|查看原发布网址|版权所有|备案号)\s*[:：]?"
    tail_noise=re.search(tail_noise_pattern,text,re.I)
    if tail_noise:
        text=text[:tail_noise.start()].rstrip(" :-—,;")

    keys = r"(?:jobCity|careerJobId|deliverBegin|deliverEnd|refreshBegin|latestProcessTime|graduationYear|salaryType|salaryMin|salaryMax|jobKeys|companyId|companyName|positionId|positionName|recruitJobName|jobName|jobOffer|eduLevel)"
    field_pattern = r'''(?:\\?["'])?''' + keys + r'''(?:\\?["'])?\s*:'''

    # A field-only JSON object/array is not a human-readable job description.
    if re.match(r'^\s*[\{\[]\s*(?:\{\s*)?' + field_pattern, text, re.I):
        return ""

    # The strongest boundary is an object opener immediately followed by a known field.
    object_match=re.search(r'\{\s*' + field_pattern, text, re.I)
    cut=object_match.start() if object_match else None

    def has_serialized_tail(match):
        suffix=text[match.end():match.end()+2200]
        next_field=re.search(field_pattern, suffix, re.I)
        if next_field and next_field.start()<1800:
            return True
        return bool(re.search(r'[}\]]\s*["\']?\s*$', suffix))

    if cut is None:
        # Handle flat/escaped JSON glued to visible text without truncating an earlier comma.
        separator_pattern=re.compile(r'[,;]\s*' + field_pattern, re.I)
        separator=next((m for m in separator_pattern.finditer(text) if has_serialized_tail(m)),None)
        if separator:
            cut=separator.start()

    if cut is None:
        # Some pages concatenate the JSON fragment on a new line without a comma.
        newline_pattern=re.compile(r'\n[ \t]*' + field_pattern, re.I)
        newline=next((m for m in newline_pattern.finditer(text) if has_serialized_tail(m)),None)
        if newline:
            cut=newline.start()

    if cut is None:
        # Last resort: if multiple known properties form a compact suffix, cut at its first key.
        first_field=re.search(field_pattern, text, re.I)
        if first_field and has_serialized_tail(first_field):
            cut=first_field.start()

    if cut is not None:
        text=text[:cut].rstrip(" \t,;\\\"'")
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

def is_stale_51job_cycle_url(url):
    """Reject explicitly year-tagged 51job campus microsites from non-2027 cycles."""
    parsed=urlparse(str(url or ""))
    host=parsed.netloc.lower()
    if not (host=="51job.com" or host.endswith(".51job.com")):
        return False
    probe=(parsed.path+" "+parsed.query).lower()
    years=re.findall(r"(?<!\d)20\d{2}(?!\d)",probe)
    if any(value!="2027" for value in years):
        return True
    # Legacy microsites sometimes abbreviate the cohort in slugs such as coach26/.
    return bool(re.search(r"(?:^|[a-z_-])2[0-6](?:届|校招|[./_-]|$)",parsed.path.lower()))

def should_discover_public_url(source_id,url):
    """Allow-list public listing/detail paths; never visit account, payment, or private areas."""
    p=urlparse(url); path=p.path.lower(); host=p.netloc.lower()
    if source_id=="yingjiesheng":
        if not same_host("https://www.yingjiesheng.com/",url): return False
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)(?:$|\?)",path): return False
        if any(x in path for x in ("/login","/register","/member","/vip","/user/","/account","/privacy","/thirdlink","/redirect")): return False
        return (path=="/" or path.startswith(("/major/","/zhuanye/","/other/","/jobs/","/job/","/joblist/","/xuanjianghui/"))
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
        # Keep public campus microsites, but reject URLs explicitly pinned to
        # a non-2027 cycle before they enter or rotate through the listing pool.
        if host.startswith("campus."): return not is_stale_51job_cycle_url(url)
        if host.startswith(("jobs.","company.")): return is_direct_detail_url(url)
        if not host.startswith("xy."): return False
        allowed_path=(path in {"/","/default-xs.php","/default.php"}
                      or path.startswith(("/jobs/","/job/","/campus/","/position/","/positions/","/jobdetail/","/job-detail/"))
                      or path.endswith((".html",".php")))
        allowed_query=any(k in p.query.lower() for k in ("page=","pageno=","jobid=","positionid=","keyword=","lang=e"))
        return allowed_path or allowed_query
    return False

def should_rotate_listing_url(source_id,url):
    """Exclude utility/error pages from rotation while preserving public job category/pagination pages."""
    if not should_discover_public_url(source_id,url):
        return False
    if source_id!="yingjiesheng":
        return True
    parsed=urlparse(str(url or ""))
    host=parsed.netloc.lower()
    path=(parsed.path or "/").lower().rstrip("/") or "/"
    if path in {"/404.htm","/404.html","/notfound","/download","/download/"} or path.startswith("/404/"):
        return False
    if host.startswith("q."):
        # Old PC intention/explanation pages are site utilities, not vacancy listings.
        if path.startswith("/pc/") or path in {"/download","/explain"}:
            return False
        return path=="/jobs/search" or path.startswith("/jobs/search/")
    if host.startswith(("web.","s.")):
        return path in {"/","/jobs","/search"} or path.startswith(("/jobs/","/job/","/search/"))
    if path.startswith("/xuanjianghui/"):
        return bool(re.search(r"/xjh-[a-z0-9-]+\.html$",path))
    # Keep profession/city and role/city job listing pages. Filter only utility/error routes above.
    return True

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
    if source_id=="yingjiesheng":
        t=re.sub(r"^\s*(?:20\d{2}|2\d)\s*(?:届\s*)?(?:校园招聘|校招|招聘)\s*[-—:：_ ]*","",t)
        if "招聘_" in t:
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
    raw=clean(title)
    t=clean_job_title(raw,source_id)
    if not is_concrete_role_title(t,source_id): return False
    # The repository is aimed at the 2027 graduate cycle. Detect stale cycle
    # markers on the raw title because clean_job_title can strip leading cohort chips.
    old_cycle=re.search(r"(?<!\d)(?:202[0-6]\s*(?:届|年\s*(?:校招|校园招聘|应届)?|校招|校园招聘)|2[0-6]\s*(?:届|校招|校园招聘))",raw)
    current_cycle=re.search(r"(?<!\d)(?:2027\s*(?:届|年\s*(?:校招|校园招聘|应届)?|校招|校园招聘)|27\s*(?:届|校招|校园招聘))",raw)
    if old_cycle and not current_cycle: return False
    if re.search(r"(?:最新更新|招聘信息|截止时间|职位列表|岗位列表|职位分类|岗位分类|专业目录)",t): return False
    if any(noise in t for noise in ("收藏","立即投递","官网投递","天截止","届秋招","全生态布局","龙头企业","内容与技术能力领先","职位推荐","职位精选","职场优选","企业墙","地点：","地点:","校招高薪榜","薪资超","HR刚","HR今日","HR在线","助力简历","投后必反馈","全场景覆盖","数字化运营与服务能力突出")): return False
    stripped=re.sub(r"(?:专业不限|可接受应届生|接受应届生|应届生优先|相关专业)$","",t).strip()
    bare=re.sub(r"[（(](?:校招|校园招聘|实习生?|应届生?|应届毕业生|20\d{2}届|\d{2}届)[）)]","",stripped)
    bare=re.sub(r"(?:校招|校园招聘|实习生?|应届生?)$","",bare).strip()
    # A role should be granular enough to distinguish the actual job from a
    # major/discipline heading. Do not publish bare category words as vacancy names.
    generic_only={"工程师","设计师","设计","销售","研发","开发","算法","数据","产品","运营","财务","市场","客服","行政","人事","专员","助理","管培生","管理培训生","经理","分析师","咨询","研究员","翻译","编辑","机械","电气","物流","技术","工程","管理","培训生","质量","机器视觉","导航算法","图像算法","感知算法","算法设计"}
    return len(stripped)>=2 and bare not in generic_only and not stripped.endswith(("类","方向","体系","专业"))

def clean_yingjiesheng_company_name(value):
    """Return the visible employer name, excluding appended vacancy-field labels."""
    company=clean(value or "")
    company=re.sub(r"^\s*[\[【（(][^\]】）)]{1,15}[\]】）)]\s*","",company)
    company=re.split(
        r"\s*(?:职位类别|职位分类|岗位类别|岗位分类|专业标签|招聘人数|招聘人数上限|工作类型|岗位性质|职位性质|"
        r"工作地点|工作城市|工作地区|工作地址|学历要求|职位描述|岗位职责|工作职责|"
        r"职位要求|岗位要求|任职要求|任职资格|职位类型|薪资待遇|薪酬福利|"
        r"发布时间|投递说明|联系方式|公司地址)\s*[:：]?",
        company,maxsplit=1,flags=re.I
    )[0]
    company=re.sub(r"\s*(?:招聘信息|校园招聘|招聘启事)$","",company)
    return re.sub(r"\s{2,}"," ",company).strip(" -—_|｜")

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
            if 2<len(title)<=45 and is_specific_role_title(title,"qiuzhaowang") and is_public_role_lead_title(title,"qiuzhaowang") and title not in result:
                result.append(title)
    return result[:40]

def listing_rotation_group(url):
    """Classify a discovered URL so one large category cannot monopolize every batch."""
    parsed=urlparse(canonical(str(url or "")))
    host=parsed.netloc.lower()
    path=(parsed.path or "/").lower()
    if host.endswith("yingjiesheng.com"):
        if host.startswith("q."):
            return "yjs-search"
        if host.startswith("zhiwei."):
            return "yjs-role-city"
        if host.startswith("m."):
            return "yjs-mobile"
        if path.startswith("/major/"):
            return "yjs-major-city"
        if path.startswith("/zhuanye/"):
            return "yjs-major-pagination" if re.search(r"/list[_-]?\d+\.html$",path) else "yjs-subject-city"
        if path.startswith("/zhiwei/"):
            return "yjs-role-city"
        if path.startswith("/xuanjianghui/"):
            return "yjs-campus-talk"
        return "yjs-other"
    first_segment=next((part for part in path.split("/") if part),"root")
    return (host or "unknown-host")+"/"+first_segment

def select_rotation_batch(pool, cursor, limit):
    """Round-robin through known URL groups while preserving order within each group."""
    unique=[]
    seen=set()
    for url in pool or []:
        value=canonical(str(url))
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    if not unique or int(limit)<=0:
        return [],max(0,int(cursor or 0))
    offset=max(0,int(cursor or 0))
    start=offset % len(unique)
    rotated=unique[start:]+unique[:start]
    count=min(len(rotated),max(1,int(limit)))
    groups={}
    group_order=[]
    for url in rotated:
        key=listing_rotation_group(url)
        if key not in groups:
            groups[key]=[]
            group_order.append(key)
        groups[key].append(url)
    if len(group_order)==1:
        return rotated[:count],offset+count
    batch=[]
    positions={key:0 for key in group_order}
    while len(batch)<count:
        progressed=False
        for key in group_order:
            pos=positions[key]
            if pos<len(groups[key]) and len(batch)<count:
                batch.append(groups[key][pos])
                positions[key]=pos+1
                progressed=True
        if not progressed:
            break
    return batch,offset+len(batch)

def extract_section(text, starts, ends, limit=5000):
    start_re="(?:"+"|".join(starts)+r")\s*[:：]?\s*"
    m=re.search(start_re,text or "",re.I)
    if not m: return ""
    tail=(text or "")[m.end():]
    positions=[]
    for end in ends:
        hit=re.search(r"\s*(?:"+end+r")\s*[:：]?\s*",tail,re.I)
        if hit: positions.append(hit.start())
    # Common footer/navigation labels must not become the final paragraph of a job field.
    footer_re=r"\s*(?:上一条|下一条|上一篇|下一篇|上一页|下一页|查看其他岗位|查看更多职位|返回顶部|登录/注册|收藏职位|职位收藏|信息来源|应届生求职网|联系方式|联系人|联系电话|联系邮箱|简历接收邮箱|简历投递|投递方式|投递邮箱|模拟面试|立即投递|投递说明|招聘流程|公司介绍|公司简介|查看原发布网址|版权所有|备案号)\s*[:：]?"
    footer=re.search(footer_re,tail,re.I)
    if footer: positions.append(footer.start())
    if positions: tail=tail[:min(positions)]
    return re.sub(r"\s+"," ",tail).strip(" :-—")[:limit].strip()

def extract_salary_text(text):
    """Extract salary only from a labelled field, explicit pay period, or real range."""
    top=(text or "")[:4500]
    n=r"\d+(?:\.\d+)?"; u=r"(?:万元|元|K|k|千|万)"
    period=r"(?:\s*/\s*(?:月|年|天)|每月|每年|每天)"
    value=rf"(?:面议|{n}\s*(?:{u}\s*)?[-—~～至到]\s*{n}\s*{u}(?:{period})?|{n}\s*{u}(?:{period})?)"
    m=re.search(rf"(?:薪资待遇|薪资|薪酬|工资|月薪|年薪|薪酬待遇|待遇)\s*[:：]?\s*({value})",top,re.I)
    if m: return re.sub(r"\s+","",m.group(1))
    m=re.search(rf"(?<![\dA-Za-z])({n}\s*(?:{u}\s*)?[-—~～至到]\s*{n}\s*{u}(?:{period})?)(?![\dA-Za-z])",top,re.I)
    if m: return re.sub(r"\s+","",m.group(1))
    m=re.search(rf"(?<![\dA-Za-z])({n}\s*{u}\s*{period})(?![\dA-Za-z])",top,re.I)
    return re.sub(r"\s+","",m.group(1)) if m else ""


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
            cm=re.search(r"(?:招聘单位|用人单位|单位名称|公司名称|企业名称|招聘企业)\s*[:：]?\s*(.{2,80}?)(?=\s*(?:职位\s*[:：]|招聘岗位|职位描述|岗位职责|发布时间|工作地点|学历要求|投递说明|公司信息|单位简介|$))",text or "",re.I)
            if cm:
                candidate=clean_yingjiesheng_company_name(cm.group(1))
                if candidate and candidate.lower() not in {"应届生求职网","招聘信息","未知","待核验"}: company=candidate
        if not company:
            m=re.search(r"(?:^|\s)([^\s]{2,40}(?:有限公司|股份有限公司|集团|研究院|研究所|大学|银行|医院|中心|公司))\s+(?:职位\s*[:：]|招聘岗位)",text or "")
            if m: company=m.group(1).strip()
    role_titles=split_role_title_candidates(title,"yingjiesheng")
    fields["role_titles"]=role_titles
    fields["title"]=role_titles[0] if role_titles else clean_job_title(title,"yingjiesheng")
    fields["company"]=clean_yingjiesheng_company_name(company)
    top=(text or "")[:3500]
    salary=extract_salary_text(top)
    if salary: fields["salary"]=salary
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    # City names in site navigation/related cards are not reliable work locations.
    # Extract only the explicit location label from this job detail.
    cities=("上海","北京","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","珠海","乌鲁木齐","洛阳","保定","廊坊","中山","佛山","温州","嘉兴","金华","常州","徐州","大连","沈阳","长春","石家庄","太原","南宁","贵阳","昆明","兰州","西宁","银川","呼和浩特","海口","三亚","泉州","汕头","惠州","江门","肇庆","清远","芜湖","扬州","南通","镇江","泰州","烟台","潍坊","淄博","临沂","济宁","日照","威海","唐山","秦皇岛","邯郸","开封","新乡","安阳","许昌","南阳","宜昌","襄阳","荆州","株洲","湘潭","衡阳","岳阳","绵阳","德阳","宜宾","泸州","乐山","遵义","大庆","齐齐哈尔","包头","鄂尔多斯","克拉玛依","喀什","阿克苏","伊犁","拉萨","林芝")
    location_pattern=(
        r"(?:工作地点|工作城市|工作地区|工作地址)\s*[:：]?\s*(.{1,100}?)"
        r"(?=\s*(?:信息来源|职位类型|专业标签|招聘岗位|学历要求|学历|"
        r"职位描述|岗位职责|工作职责|工作内容|职位诱惑|职位亮点|公司信息|企业信息|"
        r"工作地点|工作城市|工作地区|招聘人数|发布时间|投递说明|$))"
    )
    location_values=[m.group(1).strip() for m in re.finditer(location_pattern,top,re.I)]
    concrete_locations=[]
    vague_locations=[]
    for location_text in location_values:
        if location_text in ("其它","其他"):
            vague_locations.append("其它")
            continue
        if location_text in ("全国","全国各地","不限"):
            vague_locations.append("全国")
            continue
        found=[name for name in cities if re.search(re.escape(name)+r"(?:市|区|\b|$)",location_text)]
        if found:
            concrete_locations.append("、".join(dict.fromkeys(found)))
    if concrete_locations:
        fields["city"]=concrete_locations[0]
    elif vague_locations:
        fields["city"]=vague_locations[0]
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

def make_yingjiesheng_bulletin_role_lead(candidate, source_url, source_page_title,
                                                source_label, cohort, cohort_evidence=""):
    """Keep a concrete role from a 2027 YJS recruitment bulletin as a pending lead.

    The bulletin URL is shared by several positions, so it is not independent vacancy-detail
    evidence and this record must never be auto-promoted to the priority bucket.
    """
    if source_label != "应届生求职网公开岗位" or not is_direct_detail_url(source_url):
        return {}
    parsed=urlparse(source_url)
    host=(parsed.hostname or "").lower()
    if not (host=="m.yingjiesheng.com" or host.endswith(".yingjiesheng.com")):
        return {}
    heading=clean(source_page_title or "")
    if not heading or is_specific_role_title(clean_job_title(heading,"yingjiesheng"),"yingjiesheng"):
        return {}
    title=clean_job_title(candidate.get("title",""),"yingjiesheng")
    company=clean_yingjiesheng_company_name(candidate.get("company",""))
    if not is_specific_role_title(title,"yingjiesheng"):
        return {}
    if company.lower() in {"","应届生求职网","应届生求职","www.yingjiesheng.com","招聘信息","未知","待核验","待确认"}:
        return {}
    page_cohort=str(cohort or "").strip()
    if page_cohort and not page_cohort.startswith("2027"):
        return {}
    if candidate.get("cohort") and not str(candidate.get("cohort")).startswith("2027"):
        return {}
    candidate_cohort=str(candidate.get("cohort") or page_cohort).strip()
    if not candidate_cohort.startswith("2027"):
        return {}
    evidence=str(candidate.get("cohort_evidence") or cohort_evidence or "").strip()
    if not evidence:
        return {}
    item=record(company,title,canonical(source_url),source_label,candidate_cohort,3,cohort_evidence=evidence)
    item.update({
        "status":"pending_review",
        "granularity":"lead",
        "verification_score":3,
        "lead_evidence":"yingjiesheng_bulletin_role",
        "verification_status":"unknown",
        "verification_note":"岗位名称从2027届公开招聘简章的岗位表/结构化职位信息中识别；多个岗位共用简章链接，尚无独立岗位详情，不进入优先核验区",
        "last_verified":"",
        "last_verified_at":"",
        "last_collected":TODAY,
        "cohort":candidate_cohort,
        "cohort_evidence":evidence,
        "cohort_confirmed":True,
        "program":"招聘简章内识别的具体岗位线索（共享简章链接）",
        "source_page_title":heading[:220],
        "source_card_text":str(candidate.get("source_card_text") or (company+" | "+title))[:900],
    })
    for field in ("salary","degree","city","address","experience","job_type","headcount","deadline","publish_date"):
        value=candidate.get(field)
        if value not in (None,"","全国","待核验","待确认","未知"):
            item[field]=value
    return item

def extract_detail_fields_generic(text, raw_title=""):
    fields={}
    top=(text or "")[:4500]
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    salary=extract_salary_text(top)
    if salary: fields["salary"]=salary
    cities=("北京","上海","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外")
    def cities_in(segment):
        hits=[]
        for name in cities:
            # Accept district/zone suffixes such as 成都高新区、上海浦东新区 and 北京朝阳区,
            # but not arbitrary organization names such as 南京工业大学.
            pattern=r"(?:^|[\s,，、/|·\-:：省])"+re.escape(name)+r"(?:$|[\s,，、/|·\-:：区市省]|[\u4e00-\u9fff]{1,5}(?:区|县|市|州))"
            match=re.search(pattern,segment or "")
            if match:
                hits.append((match.start(),name))
        found=[]
        for _,name in sorted(hits):
            if name not in found: found.append(name)
        return found

    # Navigation headers often contain "全国 北京 上海 广州 深圳 ..." before the role.
    # Prefer an explicitly-labelled location field and stop before the next metadata section.
    location_match=re.search(
        r"(?:工作地点|工作城市|工作地区|办公地点|职位所在地|招聘城市|工作区域|工作地址)\s*[:：]?\s*(.{1,100}?)(?=\s*(?:信息来源|职位类型|专业标签|招聘岗位|学历要求|学历|职位描述|岗位职责|工作职责|工作内容|任职要求|岗位要求|职位亮点|职位诱惑|公司信息|公司介绍|上一条|下一条|Top|$))",
        top,re.I
    )
    location_text=location_match.group(1).strip() if location_match else ""
    if location_text:
        if re.match(r"^(?:全国|全国各地|不限)(?:$|[\s、,，])",location_text):
            fields["city"]="全国"
        elif re.match(r"^(?:其它|其他)(?:$|[\s、,，])",location_text):
            fields["city"]="其它"
        else:
            found_cities=cities_in(location_text)
            if found_cities: fields["city"]="、".join(found_cities)
    else:
        # When no location label exists, use only the compact role metadata immediately
        # after the pay/degree/title anchor. Never scan the whole page for arbitrary city names.
        band=""
        if salary:
            salary_pos=top.find(str(salary))
            if salary_pos>=0:
                band=top[salary_pos+len(str(salary)):min(len(top),salary_pos+len(str(salary))+500)]
        elif degree:
            degree_pos=top.find(degree)
            if degree_pos>=0: band=top[degree_pos:degree_pos+500]
        elif raw_title:
            title_pos=top.find(str(raw_title).strip())
            if title_pos>=0: band=top[title_pos+len(str(raw_title).strip()):title_pos+len(str(raw_title).strip())+500]
        section_end=re.search(r"(?:岗位职责|工作职责|工作内容|任职要求|岗位要求|职位描述|职位亮点|职位诱惑|公司信息|公司介绍|公司地址|工作地址|经验要求|工作经验|专业要求|工作时间|投递说明|投递简历)",band)
        if section_end: band=band[:section_end.start()]
        if re.search(r"(?:^|[\s,，、|:：])全国(?:$|[\s,，、|区市])",band):
            fields["city"]="全国"
        else:
            found_cities=cities_in(band)
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

def is_public_role_lead_title(title, source_id=""):
    """Reject contact copy, industry/category headings, employer names and descriptions masquerading as roles."""
    t=clean_job_title(title,source_id).strip(" -—_|｜")
    if not t or not is_specific_role_title(t,source_id):
        return False
    # Description bullets and metadata labels must never become vacancy titles.
    if re.match(r"^\s*(?:\d{1,2}|[一二三四五六七八九十])[、.．)）]",t):
        return False
    if re.match(r"^\s*(?:工作内容|岗位职责|任职要求|任职资格|岗位要求|职位描述|工作职责|岗位介绍|职位要求|工作地点|薪资待遇|职位亮点|工作说明|项目介绍|岗位说明|职位名称|岗位名称)\s*[:：]",t):
        return False
    if re.search(r"[，,。；;！？!?\n\r]",t):
        return False
    if len(t)>32:
        return False
    if "类" in t or t.endswith(("岗位","职位","计划","类别","行业")):
        return False
    if re.search(r"岗[\u4e00-\u9fff]{1,10}(?:岗|管培生|工程师|专员|经理|顾问|设计师)",t):
        return False
    role_suffix_pattern=r"(?:工程师|开发|研发|算法|管培生|运营|设计|销售|营销|市场|顾问|分析师|研究员|专员|助理|经理|测试|运维|采购|财务|法务|审计|行政|人事|客户经理|客户成功|品牌|内容|视频|影视|编辑|翻译|客服|教师|医生|护士|会计|风险|风控|投资|证券|保险|精算|质量|制造|工艺|技术支持|项目管理|投标|产品|策划)$"
    if re.search(r"\s",t) and not re.search(role_suffix_pattern,t):
        # Allow a concrete role followed by a trailing specialization in parentheses,
        # e.g. "硬件工程师 -(PCB 设计)", without admitting a second title after whitespace.
        base=re.sub(r"\s*[-—–]?\s*[（(][^（）()]*[）)]\s*$","",t).strip(" -—–")
        if not re.search(role_suffix_pattern,base):
            return False
    if re.search(r"(?:我是.{0,24}|欢迎咨询|咨询我|在线咨询|联系我们|联系我|立即沟通|查看更多岗位|查看更多职位|招聘咨询|加微信|扫码关注|投递简历)",t,re.I):
        return False
    # Recruitment plans, talent programmes, and industry taxonomy are not role-level vacancies.
    if re.search(r"(?:专项引才|引才计划|招聘计划|岗位计划|产业链|产业园|制作业|制造业|服务业|批发业|零售业|行业分类|职位类别|岗位类别|专业目录|人才计划|人才项目)",t):
        return False
    generic={"教师","咨询","设计","销售","运营","研发","算法","产品","管理","行政","财务","会计","人事","技术","数据",
             "工程师","设计师","分析师","研究员","专员","助理","经理","顾问","文案","编辑","翻译","品牌","客服","市场",
             "银行","金融","制造","实习生","应届生","企业管理咨询","专用设备制造业","IC设计","应用测试","物理管培类","质量","机器视觉","导航算法","图像算法","感知算法","算法设计",
             "研发岗位","开发岗位","市场营销类","物流运营类","财务岗人力资源岗政岗","客户成功岗营运管理岗"}
    if t in generic:
        return False
    bare=re.sub(r"^(?:AI|人工智能|互联网|信息技术|计算机|软件|硬件|自动化|机器人|大数据|数字化|智能化|智能|网络)\s*","",t,flags=re.I)
    if bare in generic:
        return False
    # Employer/site names sometimes appear in the same text column as role names. Accept them
    # only when a specific role descriptor follows the organization suffix.
    entity=re.search(r"(?:股份有限公司|有限公司|集团|研究院|研究所|设计院|大学|学院|医院|银行|证券公司|公司|中心|工作室|旅游区)",t)
    role_after=re.search(r"(?:工程师|设计师|开发岗|研发岗|算法岗|产品经理|产品专员|运营岗|销售岗|财务岗|测试岗|运维岗|管培生|顾问|分析师|研究员|专员|助理|经理|教师|护士|医生|策划师|技术支持|编辑|翻译)",t[entity.end():]) if entity else None
    if entity and not role_after:
        return False
    # A row concatenating several roles or sentence fragments isn't a single vacancy title.
    role_fragments=re.findall(r"(?:工程师|研究员|分析师|设计师|专员|助理|经理|顾问|管培生|开发岗|研发岗|算法岗|产品经理|产品专员|运营岗|销售岗|财务岗|测试岗|运维岗|客户成功岗|技术支持)",t)
    if len(role_fragments)>=3 and len(t)>17:
        return False
    # Two independent roles concatenated into one string often repeat the same job-ending noun.
    repeated_endings=("工程师","研究员","分析师","设计师","专员","助理","经理","顾问","管培生","开发岗","研发岗","算法岗","产品经理","产品专员","运营岗","销售岗","财务岗","测试岗","运维岗","客户成功岗","技术支持")
    if len(t)>8 and any(t.count(ending)>=2 for ending in repeated_endings):
        return False
    # Department/talent-category phrases without an actual role ending are not vacancies.
    if "职能" in t and not re.search(r"(?:工程师|开发|经理|顾问|专员|管培生|设计师|分析师|研究员|操作员|技术员|教师|医生|护士|会计|代表|策划师|测试|运维|采购|财务|法务|审计)",t):
        return False
    if len(re.findall(r"岗",t))>=3 and len(t)>=14:
        return False
    if t.endswith("咨询") and not re.search(r"(?:顾问|经理|专员|分析师|研究员|工程师|开发|销售|咨询师)",t):
        return False
    return True

def should_purge_qzw_noise_record(row):
    """Identify stale 秋招网 records rejected by the latest role-title policy.

    Explicit announcement/entry records are not jobs and are intentionally preserved
    in their own trust bucket. Concrete single-role titles remain in the database.
    """
    source=str(row.get("source",""))
    if "秋招网" not in source:
        return False
    if str(row.get("granularity","")) in {"announcement","entry"}:
        return False
    title=clean_job_title(row.get("title",""),"qiuzhaowang")
    return not is_public_role_lead_title(title,"qiuzhaowang")


def public_lead_dedupe_key(row):
    """Stable identity across different keyword-list URLs, retaining company and city differences."""
    return (
        str(row.get("source","")).strip().lower(),
        str(row.get("company","")).strip().lower(),
        str(row.get("title","")).strip().lower(),
        str(row.get("city","")).strip().lower(),
    )


def should_accept_yingjiesheng_detail(title, company, fields, page_text):
    """Accept a concrete role with a real employer and substantive public detail text."""
    cleaned_title=clean_job_title(title,"yingjiesheng")
    if not is_specific_role_title(cleaned_title,"yingjiesheng"):
        return False
    cleaned_company=clean_yingjiesheng_company_name(company)
    if cleaned_company.lower() in {"","应届生求职网","应届生求职","www.yingjiesheng.com","招聘信息","未知","待核验","待确认"}:
        return False
    fields=fields or {}
    for name in ("description","responsibilities","requirements"):
        value=sanitize_extracted_text(str(fields.get(name,"") or ""))
        if len(value.strip())>=45:
            return True
    return bool(fields.get("salary") and fields.get("degree") and len(clean(page_text or ""))>=180)

def source_role_metrics(rows):
    """Separate records parsed from accessible single-role detail pages from public card/list leads."""
    public_list_leads=sum(1 for row in rows if row.get("lead_evidence") in {"visible_public_job_card","yingjiesheng_public_listing","yingjiesheng_bulletin_role"})
    shared_bulletin_role_leads=sum(1 for row in rows if row.get("lead_evidence")=="yingjiesheng_bulletin_role")
    public_card_leads=sum(1 for row in rows if bool(row.get("public_card_lead")))
    direct_detail_role_records=sum(
        1 for row in rows
        if row.get("lead_evidence") not in {"visible_public_job_card","yingjiesheng_public_listing","yingjiesheng_bulletin_role"}
        and not row.get("public_card_lead")
        and row.get("granularity") not in {"announcement","entry"}
        and row.get("verification_status")=="link_alive"
        and is_direct_detail_url(row.get("source_url",""))
    )
    return {
        "public_list_leads":public_list_leads,
        "shared_bulletin_role_leads":shared_bulletin_role_leads,
        "public_card_leads":public_card_leads,
        "direct_detail_role_records":direct_detail_role_records,
    }

def extract_visible_public_role_leads(source_id, parser, source_url, record_source, page_title):
    """Return explicitly low-confidence leads from public vacancy lists when no role URL is exposed.

    These rows can only live in the pending-review bucket. They retain the list URL and
    card text as evidence; neither this function nor the publisher treats them as active
    or as having a verified per-role detail URL.
    """
    if source_id not in {"iguopin","51job"} or is_direct_detail_url(source_url):
        return []
    nodes=[clean(value) for value in (getattr(parser,"text",[]) or []) if clean(value)]
    text_all=parser.page_text()
    title_text=clean(page_title or "")
    page_context=title_text+" "+text_all
    page_cohort=year(page_context)
    page_company=""
    if source_id=="51job":
        if not page_cohort.startswith("2027") or not re.search(r"(?:校园招聘|校招)",page_context):
            return []
        match=re.search(r"^\s*(.{2,50}?)(?=20\d{2}\s*(?:届|年)?[^|｜]{0,32}(?:校园招聘|校招))",title_text)
        if match:
            brand=match.group(1).strip("【】[]（）() -—_|｜")
            page_company=brand
            for candidate in nodes[:80]:
                if brand and brand in candidate and len(candidate)<=80 and re.search(r"(?:科技|集团|公司|股份|中心|大学|研究院)",candidate) and not is_public_role_lead_title(candidate,source_id):
                    page_company=candidate
                    break
        if not page_company or page_company.lower() in {"前程无忧","前程无忧51job","51job"}:
            return []
    result=[]
    seen=set()
    blocked_exact={"职位名称","岗位名称","招聘岗位","招聘职位","招聘岗位名称","全部","详情","查看详情","查看详情+",
                   "职位类别","工作地点","发布时间","工资","福利","公司介绍","招聘岗位","校招","实习","社招",
                   "国企","央企","民营企业","本科","硕士","博士","在校生","应届生","面议","职位类型","热门岗位"}
    # On 51job job tables, prefer text after the actual "职位名称" column; other 2027
    # employer pages (for example chip/design companies) use the "招聘岗位" section.
    node_start=0
    if source_id=="51job":
        marker_indices=[i for i,value in enumerate(nodes) if value in {"职位名称","岗位名称"}]
        if marker_indices:
            node_start=marker_indices[-1]+1
        else:
            marker_indices=[i for i,value in enumerate(nodes) if value=="招聘岗位"]
            if marker_indices:
                node_start=marker_indices[-1]+1
    for i in range(node_start,len(nodes)):
        raw_title=nodes[i]
        title=clean_job_title(raw_title,source_id)
        if title in blocked_exact or not (3<=len(title)<=55):
            continue
        if any(noise in title for noise in ("申请职位","收藏","立即投递","查看更多","招聘公告","招聘信息","招聘计划","校招公告","登录/注册")):
            continue
        if not is_public_role_lead_title(title,source_id):
            continue
        if re.fullmatch(r"[\d年月日./:：-]+",title):
            continue
        context=clean(" ".join(nodes[max(0,i-2):min(len(nodes),i+11)]))[:900]
        context_year=year(context)
        if source_id=="iguopin":
            # Find the employer first, then bound the text evidence to this role card.
            # A broad lookahead can leak the next vacancy's 2027-campus marker onto
            # a neighbouring social job in the same flat list of visible text nodes.
            company=""
            company_index=-1
            for j,candidate in enumerate(nodes[i+1:min(len(nodes),i+16)],start=i+1):
                candidate=clean(candidate)
                if candidate==title or not (4<=len(candidate)<=90):
                    continue
                if is_public_role_lead_title(candidate,source_id):
                    continue
                if any(noise in candidate for noise in ("校招","社招","应届生","在校生","本科","硕士","博士","经验不限","面议","职位类型","全部","收藏","申请职位","30000人以上","10000-30000人")):
                    continue
                if re.search(r"(?:有限公司|股份|集团|银行|大学|研究院|研究所|科技|中心|医院|公司|通信|产业园)",candidate):
                    company=candidate
                    company_index=j
                    break
            if not company or company_index<0:
                continue
            context=clean(" ".join(nodes[i:company_index+1]))[:900]
            context_year=year(context)
            # Explicit social recruiting or work-experience requirements override
            # generic page banners or the next card's campus-cohort marker.
            if re.search(r"(?:社招|社会招聘)",context):
                continue
            if re.search(r"(?:\d+\s*[-—至]\s*\d+\s*年(?:经验)?|\d+\s*年以上经验)",context):
                continue
            if not re.search(r"(?:校招|应届生|应届|在校生|2027\s*届|27\s*届)",context):
                continue
            if re.search(r"(?<!\d)(?:202[0-6]|2[0-6])\s*(?:届|年应届|年校招|年校园招聘)",context) and not re.search(r"(?<!\d)2027\s*(?:届|年)?\s*(?:校招|校园招聘|应届)|(?<!\d)27\s*届",context):
                continue
            cohort=context_year if context_year.startswith("2027") else ""
            evidence="page_text" if cohort else ""
        else:
            # The page title establishes this as a current 2027 campus recruitment microsite.
            company=page_company
            cohort=page_cohort if page_cohort.startswith("2027") else ""
            evidence="page_text" if cohort else ""
        key=(company.lower(),title.lower())
        if key in seen:
            continue
        seen.add(key)
        item=record(company,title,source_url,record_source,cohort,2,cohort_evidence=evidence)
        item["status"]="pending_review"
        item["granularity"]="lead"
        item["verification_score"]=2
        item["verification_status"]="unknown"
        item["verification_note"]="公开校招/应届岗位列表中识别到具体岗位名称；原页面未提供可识别的独立岗位详情链接，需人工核验，本条不进入优先核验"
        item["lead_evidence"]="visible_public_job_card"
        item["source_page_title"]=title_text[:240]
        item["source_card_text"]=context[:700]
        item["program"]="公开列表岗位线索（未取得独立详情链接）"
        fields=extract_detail_fields_generic(context,title)
        merge_detail_fields(item,fields)
        item["cohort"]=cohort
        item["cohort_evidence"]=evidence
        item["cohort_confirmed"]=bool(evidence)
        item["last_verified"]=""
        item["last_verified_at"]=""
        item["last_collected"]=TODAY
        result.append(item)
        if len(result)>=35:
            break
    return result

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

def extract_yingjiesheng_card_fields(raw_title, links, role_index, page_url="", tables=None, table_links=None):
    """Read only the visible company/title/card metadata next to a public YJS vacancy link."""
    title=clean_job_title(raw_title,"yingjiesheng")
    if not is_specific_role_title(title,"yingjiesheng") or role_index<0 or role_index>=len(links):
        return {}
    target=canonical(urljoin(page_url,links[role_index].get("href","")))
    company_markers=("有限公司","股份","集团","公司","科技","大学","研究院","研究所","医院","银行","证券","中心","学院","制药","生物","机械","电子","汽车","文化传媒")
    city_names=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","全国","海外")
    blocked={"应届生求职网","招聘信息","职位描述","职位要求","查看详情","立即投递","更多岗位","职位列表","最新招聘"}
    def clean_company(value):
        value=clean_yingjiesheng_company_name(value)
        if not (2<=len(value)<=75) or value.lower() in blocked or value==title:
            return ""
        if is_specific_role_title(value,"yingjiesheng") or any(x in value for x in NAV_BAD):
            return ""
        if any(city in value for city in city_names) and not any(mark in value for mark in company_markers):
            return ""
        if re.search(r"(?:202[0-9]|2[0-9])\s*(?:届|校招)|实习|社招|应届|校园招聘|招聘对象|工作地点",value):
            return ""
        return value if any(mark in value for mark in company_markers) else ""
    card_text=clean(raw_title)
    company=""
    # Prefer the actual table row containing this role link.
    for row_index,row_links in enumerate(table_links or []):
        urls=[canonical(urljoin(page_url,href)) for href in (row_links or [])]
        if target not in urls or row_index>=len(tables or []):
            continue
        cells=[clean(cell) for cell in tables[row_index] if clean(cell)]
        card_text=" | ".join(cells)[:1200]
        company=next((candidate for cell in cells if (candidate:=clean_company(cell))),"")
        if company:
            break
    # Otherwise use adjacent visible company-name links; do not infer employer from the site name.
    if not company:
        nearby=list(range(role_index+1,min(len(links),role_index+6)))+list(range(role_index-1,max(-1,role_index-5),-1))
        for idx in nearby:
            link=links[idx]
            value=clean(link.get("text",""))
            href=canonical(urljoin(page_url,link.get("href","")))
            if not value or is_direct_detail_url(href):
                continue
            candidate=clean_company(value)
            if candidate:
                company=candidate
                card_text=(card_text+" | "+value)[:1200]
                break
    if not company:
        return {}
    fields={"title":title,"company":company,"source_card_text":card_text[:900]}
    cohort=year(raw_title+" "+card_text)
    if cohort and not cohort.startswith("2027"):
        return {}
    if cohort:
        fields["cohort"]=cohort
        fields["cohort_evidence"]="page_text"
        fields["cohort_confirmed"]=True
    visible=extract_detail_fields_generic(card_text,title)
    for key in ("salary","degree","city","address","experience","job_type","headcount","deadline","publish_date"):
        if visible.get(key) not in ("",None,"全国","待核验"):
            fields[key]=visible[key]
    return fields

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

def merge_generic_detail_fallback(primary, fallback):
    """Fill missing fields; let a precise city replace a vague 全国/其它 placeholder."""
    result=dict(primary or {})
    vague_locations={"全国","其它","其他"}
    for key,value in (fallback or {}).items():
        if value in (None,"",[],{}):
            continue
        current=result.get(key)
        if not current:
            result[key]=value
        elif key=="city" and isinstance(current,str) and isinstance(value,str):
            if current.strip() in vague_locations and value.strip() not in vague_locations:
                result[key]=value
    return result

def fields_for_job_record(fields):
    """Drop parser-only identity fields after all dedicated and generic extraction is finished."""
    return {key:value for key,value in (fields or {}).items() if key not in {"title","role_titles","company"}}

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
      "program":program,"collector":"public-html-v5.21","granularity":"job" if status=="active" else "review",
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
    page_heading=clean(getattr(parser,"page_title",""))
    yjs_bulletin_company=clean_yingjiesheng_company_name(company)
    yjs_shared_bulletin=(
        source_label=="应届生求职网公开岗位"
        and is_direct_detail_url(source)
        and str(cohort or "").startswith("2027")
        and bool(cohort_evidence)
        and not is_specific_role_title(clean_job_title(page_heading,"yingjiesheng"),"yingjiesheng")
        and yjs_bulletin_company.lower() not in {"","应届生求职网","应届生求职","招聘信息","未知","待核验","待确认"}
    )
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
            if candidates and not yjs_shared_bulletin:
                employer=candidates[0]
                employer=re.split(r"\s+(?:金融|互联网/科技/AI|中国大陆|香港|美国|英国|新加坡|其他|金融/银行/证券|生产制造/工业|汽车/新能源|物流/供应链|快消/零售|生物医药/制药|教育/培训|房地产/建筑|咨询/四大|外企/合资|私企/民企|央国企/事业单位)\b",employer,1)[0].strip()
                employer=re.sub(r"(世界500强|知名互联网|独角兽|半导体大厂|头部外企|新势力车企|大模型公司|行业领先|上市公司|福利完善|成长空间大|团队氛围好).*$","",employer).strip(" ·-_")
            if not employer or (employer==company and not yjs_shared_bulletin): continue
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
        if yjs_shared_bulletin:
            r["source_card_text"]=joined[:900]
            r["program"]="招聘简章内岗位表（共享简章链接）"
        out.append(r)
    return out

def collect_source(src):
    company,root=src["company"],src["url"]; max_pages=int(src.get("max_pages",6)); max_candidates=int(src.get("max_candidates",500)); max_direct_detail_pages=int(src.get("max_direct_detail_pages",max_pages)); queue=list(src.get("seed_urls") or [root])[:max_pages]; visited=set(); seen=set(); rows=[]; qzw_detail_metadata={}; shared_bulletin_seen=set()
    diagnostics={"pages_attempted":0,"pages_failed":0,"html_bytes":0,"links_seen":0,"tables_seen":0,"embedded_json_blocks":0,"role_like_links":0,"discovery_pages_queued":0,"listing_pages_queued":0,"listing_url_candidates":[],"listing_urls_visited":[],"link_samples":[],"link_sample_urls":set(),"direct_detail_pages_visited":0,"direct_detail_links_queued":0,"direct_detail_links_skipped_access_limit":0,"direct_detail_requests_attempted":0,"direct_detail_title_candidates":0,"direct_detail_title_rejected":0,"direct_detail_score_gate_recovered":0,"direct_detail_score_gate_skipped":0,"direct_detail_role_rows_emitted":0,"detail_access_limited":0,"detail_access_limited_samples":[],"public_detail_leads":0,"shared_bulletin_role_leads":0,"direct_detail_links_deferred_by_budget":0,"detail_redirects":0,"detail_redirect_samples":[],"direct_detail_samples":[],"browser_render_attempts":0,"browser_render_successes":0,"browser_render_blocked":0,"browser_rendered_body_chars":0,"browser_rendered_links":0,"browser_render_samples":[],"sample_title":"","last_error":"","parser_modes":[]}
    queue.sort(key=lambda value:discovery_priority(src.get("id",""),value))
    while queue and len(visited)<max_pages and len(rows)<max_candidates:
        rows_before_page=len(rows)
        page=queue.pop(0)
        cp=(page.split("#",1)[0] if src.get("id")=="qiuzhaowang" else canonical(page))
        if src.get("id")=="yingjiesheng" and is_direct_detail_url(page) and diagnostics["direct_detail_requests_attempted"]>=max_direct_detail_pages:
            diagnostics["direct_detail_links_deferred_by_budget"]+=1
            continue
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
        if source_id=="yingjiesheng" and is_direct_detail_url(final):
            challenge_payload=(
                bool(re.search(r'''["']l1["']\s*:\s*["']var\s+arg1\s*=''',html,re.I))
                and bool(re.search(r'''["']l2["']\s*:\s*["']GET["']''',html,re.I))
                and not bool(re.search(r"<(?:!doctype\s+html|html|body|title|h1)\b",html,re.I))
            )
            if challenge_payload:
                diagnostics["detail_access_limited"]+=1
                diagnostics["last_error"]="Some public YJS detail URLs returned an access-verification payload; not bypassed"
                if len(diagnostics["detail_access_limited_samples"])<8:
                    diagnostics["detail_access_limited_samples"].append({
                        "requested_path":urlparse(page).path,
                        "final_path":urlparse(final).path,
                        "body_chars":len(html),
                        "reason":"access_verification_payload"
                    })
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
        original_page_title=raw_page_title
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
        if source_id in {"iguopin","51job"} and not is_direct_detail_url(final):
            rows.extend(extract_visible_public_role_leads(source_id,p,final,record_source,raw_page_title))
        detail_role_titles=list(detail_fields.get("role_titles") or []) if src.get("id")=="yingjiesheng" else []
        if src.get("id")=="nowcoder":
            page_title=clean_job_title(detail_heading or p.page_title,"nowcoder")
        if is_direct_detail_url(final):
            generic_fallback=extract_detail_fields_generic(page_text,detail_heading or raw_page_title)
            detail_fields=merge_generic_detail_fallback(detail_fields,generic_fallback)
        # Build the publishable snapshot only after generic fallback values have been merged.
        detail_fields_for_merge=fields_for_job_record(detail_fields)
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
            is_yjs_direct=(src.get("id")=="yingjiesheng" and is_direct_detail_url(final))
            if is_yjs_direct:
                diagnostics["direct_detail_title_candidates"]+=1
            page_title_ok=is_specific_role_title(candidate_page_title,src.get("id","")) if "公开" in record_source else is_concrete_role_title(candidate_page_title,src.get("id",""))
            if src.get("id")=="boss" and not (detail_fields.get("company") or (page_company and page_company not in {company,"BOSS直聘"})):
                page_title_ok=False
            if not candidate_page_title or not page_title_ok:
                if is_yjs_direct:
                    diagnostics["direct_detail_title_rejected"]+=1
                continue
            sc=score(candidate_page_title,final,page_text)
            if is_yjs_direct and sc<5:
                if should_accept_yingjiesheng_detail(candidate_page_title,page_company,detail_fields,page_text):
                    sc=5
                    diagnostics["direct_detail_score_gate_recovered"]+=1
                else:
                    diagnostics["direct_detail_score_gate_skipped"]+=1
            if sc>=5 and is_direct_detail_url(final):
                if is_yjs_direct:
                    diagnostics["direct_detail_role_rows_emitted"]+=1
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
                # If the detail URL is restricted, retain only a public listing lead whose
                # visible card identifies both a concrete title and a real employer.
                scheduled_now={canonical(value) for value in queue}
                for candidate in parsed_rows:
                    candidate_url=canonical(candidate.get("source_url",""))
                    if not candidate_url:
                        continue
                    if candidate_url==canonical(final):
                        if source_id=="yingjiesheng":
                            lead=make_yingjiesheng_bulletin_role_lead(
                                candidate,final,original_page_title,record_source,cohort,cohort_evidence
                            )
                            if lead:
                                dedupe_key=(str(lead.get("company","")).strip().lower(),
                                            str(lead.get("title","")).strip().lower(),
                                            str(lead.get("city","")).strip().lower(),
                                            str(lead.get("cohort","")).strip().lower())
                                if dedupe_key not in shared_bulletin_seen:
                                    shared_bulletin_seen.add(dedupe_key)
                                    rows.append(lead)
                                    diagnostics["public_detail_leads"]+=1
                                    diagnostics["shared_bulletin_role_leads"]+=1
                        continue
                    if not is_direct_detail_url(candidate_url):
                        continue
                    if source_id=="yingjiesheng":
                        candidate_title=clean_job_title(candidate.get("title",""),"yingjiesheng")
                        candidate_company=clean_yingjiesheng_company_name(candidate.get("company",""))
                        candidate_cohort=str(candidate.get("cohort") or cohort or "")
                        if candidate_cohort and not candidate_cohort.startswith("2027"):
                            continue
                        host=urlparse(candidate_url).netloc.lower()
                        if (is_specific_role_title(candidate_title,"yingjiesheng")
                                and candidate_company not in {"","应届生求职网","招聘信息","未知","待确认"}
                                and (host=="m.yingjiesheng.com" or host.endswith(".yingjiesheng.com"))):
                            lead=record(candidate_company,candidate_title,candidate_url,record_source,
                                        candidate.get("cohort") or "",3,
                                        cohort_evidence=candidate.get("cohort_evidence") or "")
                            lead.update({
                                "status":"pending_review","granularity":"lead","verification_score":3,
                                "lead_evidence":"yingjiesheng_public_listing",
                                "verification_status":"unknown","verification_note":"公开分类/列表页可识别到具体岗位、雇主和岗位级链接；详情页需要独立核验",
                                "last_verified":"","last_collected":TODAY,
                                "source_page_title":raw_page_title[:220],
                                "source_card_text":str(candidate.get("source_card_text") or (candidate_company+" | "+candidate_title))[:900]
                            })
                            for field in ("salary","degree","city","address","experience","job_type","headcount","deadline","publish_date"):
                                value=candidate.get(field)
                                if value not in ("",None,"全国","待核验"):
                                    lead[field]=value
                            rows.append(lead)
                            diagnostics["public_detail_leads"]+=1
                    if candidate_url in visited or candidate_url in scheduled_now:
                        continue
                    if len(queue)>=max_pages:
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
            if src.get("id")=="yingjiesheng" and not should_rotate_listing_url(source_id,discovery_url): continue
            is_detail=is_direct_detail_url(discovery_url)
            if source_id=="boss" and is_detail:
                # BOSS vacancy links consistently redirect to a security interstitial;
                # retain the card lead, but don't spend the page budget retrying detail URLs.
                diagnostics["direct_detail_links_skipped_access_limit"]+=1
                continue
            if (not is_detail) and src.get("id") in {"yingjiesheng","nowcoder","boss","51job","iguopin"}:
                candidates=diagnostics["listing_url_candidates"]
                if (should_rotate_listing_url(source_id,discovery_url)
                        and discovery_url not in candidates
                        and len(candidates)<int(src.get("listing_pool_limit",1200))):
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
                if src.get("id")=="yingjiesheng" and is_direct_detail_url(href):
                    card=extract_yingjiesheng_card_fields(a.get("text",""),p.links,link_index,final,p.tables,p.table_links)
                    if card:
                        company_name=card.pop("company")
                        clean_title=card.pop("title")
                        item=record(company_name,clean_title,href,record_source,card.get("cohort") or "",3,
                                    cohort_evidence=card.get("cohort_evidence",""))
                        item.update({
                            "status":"pending_review","granularity":"lead","verification_score":3,
                            "lead_evidence":"yingjiesheng_public_listing",
                            "verification_status":"unknown","verification_note":"公开列表卡片可见且有岗位级链接；详情页面需要独立核验",
                            "source_page_title":raw_page_title[:220],
                            "source_card_text":card.get("source_card_text","")[:900],
                            "last_collected":TODAY,"last_verified":"","confirmed_by":[record_source]
                        })
                        for key in ("salary","degree","city","address","experience","job_type","headcount","deadline","publish_date"):
                            if card.get(key) not in ("",None,"全国","待核验"):
                                item[key]=card[key]
                        rows.append(item)
                        diagnostics["public_detail_leads"]+=1
                        seen.add(href)
                    # Queue the public direct link only; if it returns a security payload,
                    # it is counted as restricted and is never bypassed.
                    if href not in visited and href not in queue and len(queue)<max_pages:
                        queue.append(href)
                        diagnostics["discovery_pages_queued"]+=1
                        diagnostics["direct_detail_links_queued"]+=1
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
                        item["public_card_lead"]=True
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
    # De-duplicate list-only leads across keyword/pagination URLs.
    best={}
    for r in rows:
        if r.get("lead_evidence")=="visible_public_job_card":
            k=("public-list-lead",)+public_lead_dedupe_key(r)
        elif r.get("lead_evidence")=="yingjiesheng_bulletin_role":
            k=("yingjiesheng-bulletin-lead",)+public_lead_dedupe_key(r)+(str(r.get("cohort","")).strip().lower(),)
        else:
            k=("record",r["company"],r["title"],r["source_url"])
        rank=(int(r.get("verification_score",0)),bool(r.get("cohort_evidence")),
              bool(r.get("salary") or r.get("degree")),len(str(r.get("source_card_text",""))))
        prior=best.get(k)
        if prior is None:
            best[k]=r
        else:
            prior_rank=(int(prior.get("verification_score",0)),bool(prior.get("cohort_evidence")),
                        bool(prior.get("salary") or prior.get("degree")),len(str(prior.get("source_card_text",""))))
            if rank>prior_rank:
                best[k]=r
    best_rows=list(best.values())
    role_rows=[r for r in best_rows if r.get("granularity") not in {"announcement","entry"}]
    announcement_rows=[r for r in best_rows if r.get("granularity")=="announcement"]
    entry_rows=[r for r in best_rows if r.get("granularity")=="entry"]
    diagnostics["role_records"]=len(role_rows)
    diagnostics["announcement_records"]=len(announcement_rows)
    diagnostics["portal_entries"]=len(entry_rows)
    if src.get("id")=="yingjiesheng" and diagnostics.get("detail_access_limited",0)>0 and any(r.get("lead_evidence") in {"yingjiesheng_public_listing","yingjiesheng_bulletin_role"} for r in role_rows):
        diagnostics["source_status"]="public_list_leads_detail_restricted"
    elif src.get("id")=="boss" and role_rows and diagnostics["direct_detail_links_skipped_access_limit"]>0 and diagnostics["direct_detail_pages_visited"]==0:
        diagnostics["source_status"]="public_card_leads_only"
    elif src.get("id") in {"iguopin","51job"} and role_rows and all(r.get("lead_evidence")=="visible_public_job_card" for r in role_rows):
        diagnostics["source_status"]="public_list_leads_only"
    else:
        diagnostics["source_status"]=("ok" if role_rows else
            ("detail_access_restricted" if diagnostics.get("direct_detail_requests_attempted",0)>0 and diagnostics.get("direct_detail_pages_visited",0)==0 and diagnostics.get("detail_redirects",0)>0 else
             ("announcement_only" if announcement_rows else
              ("entry_only" if entry_rows else
               ("listing_only_no_job_details" if diagnostics["listing_pages_queued"] and visited else
                ("reachable_no_records" if visited else "access_error"))))))
    diagnostics["parser_modes"]=[name for name,count in (("links",diagnostics["links_seen"]),("tables",diagnostics["tables_seen"]),("embedded_json",diagnostics["embedded_json_blocks"])) if count]
    return best_rows,visited,diagnostics

def parse_source_ids(value):
    """Parse an optional comma/space-delimited source allow-list for focused collection."""
    return {part.strip().lower() for part in re.split(r"[,;\s]+", str(value or "")) if part.strip()}

def select_enabled_sources(sources, requested_ids=None):
    """Choose enabled public sources; reject unknown/disabled IDs rather than silently running nothing."""
    all_enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
    requested={str(value).strip().lower() for value in (requested_ids or set()) if str(value).strip()}
    known={str(s.get("id","")).strip().lower() for s in sources}
    unknown=requested-known
    if unknown:
        raise ValueError("Unknown source IDs requested: "+", ".join(sorted(unknown)))
    selected=[s for s in all_enabled if str(s.get("id","")).strip().lower() in requested] if requested else all_enabled
    unavailable=requested-{str(s.get("id","")).strip().lower() for s in selected}
    if unavailable:
        raise ValueError("Requested sources are not enabled public sources: "+", ".join(sorted(unavailable)))
    return all_enabled,selected

def main():
    sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
    requested_source_ids=parse_source_ids(os.environ.get("QIUZHAO_SOURCE_IDS",""))
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
        if requested_source_ids and source_id not in requested_source_ids: continue
        if source_id not in rotating_sources: continue
        base_seeds=[normalize_source_url(source_id,u) for u in (source.get("seed_urls") or [source.get("url","")])]
        base_set={normalize_source_url(source_id,u) for u in base_seeds if u}
        stored_pool=[]
        stale_cycle_urls_pruned=sum(1 for url in state["source_listing_url_pool"].get(source_id,[]) if source_id=="51job" and is_stale_51job_cycle_url(url))
        for url in state["source_listing_url_pool"].get(source_id,[]):
            normalized=normalize_source_url(source_id,str(url))
            if normalized and normalized not in base_set and not is_direct_detail_url(normalized) and should_rotate_listing_url(source_id,normalized) and normalized not in stored_pool:
                stored_pool.append(normalized)
        cursor=int(state["source_listing_cursor"].get(source_id,0))
        batch_size=int(source.get("rotation_seed_batch",12))
        pool_limit=int(source.get("listing_pool_limit",600))
        batch,next_cursor=select_rotation_batch(stored_pool,cursor,batch_size)
        source["seed_urls"]=list(dict.fromkeys(base_seeds+batch))[:int(source.get("max_pages",80))]
        batch_groups={}
        for batch_url in batch:
            group=listing_rotation_group(batch_url)
            batch_groups[group]=batch_groups.get(group,0)+1
        rotation_meta[source_id]={"base_seeds":base_seeds,"pool":stored_pool,"cursor":cursor,"batch":batch,"batch_groups":batch_groups,"next_cursor":next_cursor,"batch_size":batch_size,"pool_limit":pool_limit,"stale_cycle_urls_pruned":stale_cycle_urls_pruned}
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
        if source.get("id")=="qiuzhaowang" and (not requested_source_ids or "qiuzhaowang" in requested_source_ids):
            source["seed_urls"]=[f"https://qiuzhaowang.com/latest?page={n}&year=2027" for n in qiuzhaowang_pages]
            source["max_pages"]=50
            source["max_candidates"]=6000
    all_enabled,enabled=select_enabled_sources(sources,requested_source_ids)
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
                            and should_rotate_listing_url(source_id,normalized) and normalized not in pool):
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
                diag["stale_cycle_urls_pruned"]=meta.get("stale_cycle_urls_pruned",0)
                diag["listing_urls_visited_count"]=len(diag.get("listing_urls_visited",[]))
            all_rows.extend(rows)
            a=sum(r["status"]=="active" for r in rows); rr=sum(r["status"]!="active" for r in rows)
            active+=a; review+=rr
            source_metrics=source_role_metrics(rows)
            results.append({"id":s["id"],"company":s["company"],"pages_visited":len(visited),"records_found":len(rows),
                            "active_candidates":a,"pending_review":rr,
                            "pages_attempted":diag.get("pages_attempted",0),"pages_failed":diag.get("pages_failed",0),
                            "html_bytes":diag.get("html_bytes",0),"links_seen":diag.get("links_seen",0),
                            "listing_url_candidates":len(diag.get("listing_url_candidates",[])),
                            "listing_urls_visited":diag.get("listing_urls_visited_count",len(diag.get("listing_urls_visited",[]))),
                            "listing_pool_size":diag.get("listing_pool_size",0),
                            "stale_cycle_urls_pruned":diag.get("stale_cycle_urls_pruned",0),
                            "listing_cursor_next":state.get("source_listing_cursor",{}).get(s.get("id",""),0),
                            "listing_rotation_groups":rotation_meta.get(s.get("id",""),{}).get("batch_groups",{}),
                            "tables_seen":diag.get("tables_seen",0),"embedded_json_blocks":diag.get("embedded_json_blocks",0),
                            "role_like_links":diag.get("role_like_links",0),"discovery_pages_queued":diag.get("discovery_pages_queued",0),
                            "listing_pages_queued":diag.get("listing_pages_queued",0),"direct_detail_pages_visited":diag.get("direct_detail_pages_visited",0),"direct_detail_links_queued":diag.get("direct_detail_links_queued",0),"direct_detail_links_skipped_access_limit":diag.get("direct_detail_links_skipped_access_limit",0),"direct_detail_requests_attempted":diag.get("direct_detail_requests_attempted",0),"direct_detail_title_candidates":diag.get("direct_detail_title_candidates",0),"direct_detail_title_rejected":diag.get("direct_detail_title_rejected",0),"direct_detail_score_gate_recovered":diag.get("direct_detail_score_gate_recovered",0),"direct_detail_score_gate_skipped":diag.get("direct_detail_score_gate_skipped",0),"direct_detail_role_rows_emitted":diag.get("direct_detail_role_rows_emitted",0),"detail_access_limited":diag.get("detail_access_limited",0),"detail_access_limited_samples":diag.get("detail_access_limited_samples",[]),"public_detail_leads":diag.get("public_detail_leads",0),"shared_bulletin_role_leads":diag.get("shared_bulletin_role_leads",0),"direct_detail_links_deferred_by_budget":diag.get("direct_detail_links_deferred_by_budget",0),"direct_detail_page_budget":int(s.get("max_direct_detail_pages",s.get("max_pages",0))),"detail_redirects":diag.get("detail_redirects",0),"detail_redirect_samples":diag.get("detail_redirect_samples",[]),"direct_detail_samples":diag.get("direct_detail_samples",[]),"browser_render_attempts":diag.get("browser_render_attempts",0),"browser_render_successes":diag.get("browser_render_successes",0),
                            "browser_render_blocked":diag.get("browser_render_blocked",0),"browser_rendered_body_chars":diag.get("browser_rendered_body_chars",0),
                            "browser_rendered_links":diag.get("browser_rendered_links",0),"browser_render_samples":diag.get("browser_render_samples",[]),
                            "role_records":diag.get("role_records",0),
                            "public_list_leads":source_metrics["public_list_leads"],
                            "shared_bulletin_role_leads":source_metrics["shared_bulletin_role_leads"],
                            "public_card_leads":source_metrics["public_card_leads"],
                            "direct_detail_role_records":source_metrics["direct_detail_role_records"],
                            "announcement_records":diag.get("announcement_records",0),"portal_entries":diag.get("portal_entries",0),
                            "sample_title":diag.get("sample_title",""),
                            "role_title_samples":[r.get("title","") for r in rows if r.get("granularity") not in {"announcement","entry"}][:8],
                            "role_field_samples":[{"title":r.get("title",""),"company":r.get("company",""),"city":r.get("city",""),"salary":r.get("salary",""),"degree":r.get("degree",""),"source_url":r.get("source_url",""),"field_lengths":{field:len(str(r.get(field,"") or "")) for field in ("description","responsibilities","requirements","benefits")} } for r in rows if r.get("granularity") not in {"announcement","entry"}][:5],
                            "link_samples":[{k:v for k,v in item.items() if k!="_priority"} for item in diag.get("link_samples",[])],
                            "last_error":diag.get("last_error",""),"parser_modes":diag.get("parser_modes",[]),
                            "source_status":diag.get("source_status","access_error"),"last_run_at":STAMP,"run_scope":("source_subset" if requested_source_ids else "all_enabled_sources")})
    results.sort(key=lambda x:x["company"])
    rot_seen=set()
    for visited_url in qiuzhaowang_visited:
        match=re.search(r"[?&]page=(\d+)",visited_url)
        if match and int(match.group(1)) in rotation_pages: rot_seen.add(int(match.group(1)))
    next_page=rotation_start
    while next_page in rot_seen:
        next_page+=1
        if next_page>192: next_page=11
    if not requested_source_ids or "qiuzhaowang" in requested_source_ids:
        state["qiuzhaowang_next_page"]=next_page
        state["qiuzhaowang_last_pages"]=qiuzhaowang_pages
    STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    INBOX.write_text(json.dumps(all_rows,ensure_ascii=False,indent=2),encoding="utf-8")
    prior_manifest={}
    prior_results=[]
    if requested_source_ids:
        try:
            prior_manifest=json.loads(MANIFEST.read_text(encoding="utf-8"))
            prior_results=prior_manifest.get("results",[]) or []
        except Exception:
            prior_manifest={}; prior_results=[]
    manifest_payload=prior_manifest.copy() if requested_source_ids else {}
    previous_pagination=prior_manifest.get("pagination_state",{}) if requested_source_ids else {}
    rotation_payload=dict(previous_pagination.get("source_listing_rotation",{}) or {})
    rotation_payload.update({sid:{
        "known_public_list_urls":len(state["source_listing_url_pool"].get(sid,[])),
        "next_offset":state["source_listing_cursor"].get(sid,0),
        "batch_size":rotation_meta[sid]["batch_size"]
    } for sid in rotation_meta})
    pagination_payload={
        "qiuzhaowang_pages":(qiuzhaowang_pages if (not requested_source_ids or "qiuzhaowang" in requested_source_ids) else previous_pagination.get("qiuzhaowang_pages",qiuzhaowang_pages)),
        "qiuzhaowang_next_page":(next_page if (not requested_source_ids or "qiuzhaowang" in requested_source_ids) else previous_pagination.get("qiuzhaowang_next_page",next_page)),
        "source_listing_rotation":rotation_payload
    }
    manifest_payload.update({
     "run_date":TODAY,
     "collector_version":"5.21",
     "enabled_sources":len(all_enabled),
     "collected_sources":len(enabled),
     "run_scope":"source_subset" if requested_source_ids else "all_enabled_sources",
     "run_scope_source_ids":sorted(requested_source_ids) if requested_source_ids else sorted(s.get("id","") for s in enabled),
     "pagination_state":pagination_payload,
     "records_found":len(all_rows),
     "records_found_this_run":len(all_rows),
     "active_candidates":active,
     "pending_review":review,
     "results":results,
     "policy":"Public official pages only; no login/CAPTCHA/private API/anti-bot bypass.",
     "granularity":"Only high-confidence concrete role records are active; program/announcement/navigation pages remain pending_review."
    })
    if requested_source_ids:
        merged_results={row.get("id"):row for row in prior_results if row.get("id") not in requested_source_ids}
        merged_results.update({row.get("id"):row for row in results})
        manifest_payload["results"]=sorted(merged_results.values(),key=lambda row:row.get("company",""))
    MANIFEST.write_text(json.dumps(manifest_payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"sources={len(enabled)}/{len(all_enabled)} scope={manifest_payload.get('run_scope')} records={len(all_rows)} active_candidates={active} pending_review={review}")
    

if __name__ == "__main__":
    main()
