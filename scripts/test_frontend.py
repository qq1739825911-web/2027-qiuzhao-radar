"""Static and JavaScript syntax smoke tests for the published single-page app."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "index.html"
source = HTML.read_text(encoding="utf-8")

checks = {
    "job detail modal exists": 'id="jobDetailModal"' in source,
    "job title opens the in-site detail modal": 'data-detail-id="' in source and "openJobDetail(" in source,
    "search index is built from current normalized job fields": 'DATA=DATA.map(row=>({...row,searchText:[row.company,row.title,row.keywords,row.category,row.city,row.type,row.degree,row.program,row.cohort,row.salary,row.address,row.description,row.requirements,row.responsibilities,row.benefits,row.company_info].join(" ").toLocaleLowerCase()}));' in source,
    "keyword filter retains a fallback over visible job fields": 'x.searchText||[x.company,x.title,x.keywords,x.category,x.city,x.type,x.degree,x.program,x.cohort,x.salary,x.address].join(" ").toLocaleLowerCase()' in source,
    "job detail text sanitization exists": "function sanitizeDetailField" in source,
    "lazy rendering of company groups exists": 'data-loaded="false"' in source and 'data-company-index="' in source,
    "review bucket fallback exists": 'const emptyBox=$("jobs").querySelector(".empty")' in source,
    "official recruitment route is distinguished": "function usesOfficialRecruitmentFlow" in source,
    "close button has accessible label": 'aria-label="关闭详情"' in source,
    "reset control exists": 'id="reset"' in source,
    "fast filters cancel a pending keyword render": 'else{clearTimeout(searchDebounce);searchDebounce=null;render()}' in source,
    "exact-title search results rank ahead of description matches": "const searchNeedle=" in source and "relevance(y)-relevance(x)" in source,
    "source productivity is reported separately from reachability": "岗位级记录 " in source and "只有公告/入口 " in source and "可访问但零产出 " in source,
    "BOSS public-card leads are not labelled as failed sources": "public_card_leads_only" in source and "公开卡片线索，详情访问受限" in source and "BOSS公开卡片线索" in source,
    "search input is wired to a debounced renderer": 'setTimeout(render,140)' in source and 'addEventListener("input"' in source,
    "filter reset button has a handler": '$("reset").onclick=' in source,
    "tab navigation has click handlers": 't.onclick=()=>switchTab(t.dataset.tab)' in source,
    "modal close and escape handlers exist": 'b.onclick=()=>closeJobDetail(true)' in source and 'e.key==="Escape"' in source,
    "favorites and application status changes have handlers": 'b.onclick=()=>toggle(b.dataset.id)' in source and 'x.onchange=()=>{apps[x.dataset.app]=x.value' in source,
    "favorite and delivery board handlers exist": "function renderBoard()" in source and 'data-app="' in source,
}
for name, okay in checks.items():
    if not okay:
        raise AssertionError("FAILED: " + name)
    print("PASS:", name)

scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", source, flags=re.I | re.S)
if len(scripts) != 1:
    raise AssertionError(f"Expected exactly one inline script, found {len(scripts)}")
with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as handle:
    handle.write(scripts[0])
    js_path = Path(handle.name)
try:
    result = subprocess.run(["node", "--check", str(js_path)], text=True, capture_output=True)
    if result.returncode:
        raise AssertionError("JavaScript syntax check failed:\n" + result.stdout + result.stderr)
    print("PASS: inline JavaScript parses with node --check")
finally:
    js_path.unlink(missing_ok=True)

helper_start = scripts[0].find("function sanitizeDetailField")
helper_end = scripts[0].find("function setup(){", helper_start)
if helper_start < 0 or helper_end < 0:
    raise AssertionError("Could not isolate pure detail helpers for runtime tests")
helper_code = scripts[0][helper_start:helper_end]
helper_test = r'''
const assert = require("node:assert/strict");
''' + helper_code + r'''
const dirty = "岗位职责：完成测试并整理报告。\n3. 跟踪结果。\",\"jobCity\":\"北京\",\"careerJobId\":11025,\"salaryMax\":9999999";
const clean = sanitizeDetailField(dirty);
assert.equal(clean, "岗位职责：完成测试并整理报告。\n3. 跟踪结果。");
assert.equal(sanitizeDetailField("{\"jobCity\":\"北京\",\"careerJobId\":11025}"), "");
assert.equal(cleanUiTitle("【27届校招】测试工程师(A54426)"), "测试工程师");
assert.equal(cleanUiTitle("〖27届校招〗具身智能AI软件工程师"), "具身智能AI软件工程师");
assert.equal(usesOfficialRecruitmentFlow({company:"中国建设银行",type:"银行"}), true);
assert.equal(usesOfficialRecruitmentFlow({company:"上海基美影业股份有限公司",type:"民营"}), false);
console.log("PASS: sanitizer, title cleanup and employer route helpers");
'''
with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as handle:
    handle.write(helper_test)
    helper_path = Path(handle.name)
try:
    result = subprocess.run(["node", str(helper_path)], text=True, capture_output=True)
    if result.returncode:
        raise AssertionError("Frontend helper runtime tests failed:\n" + result.stdout + result.stderr)
    print(result.stdout.strip())
finally:
    helper_path.unlink(missing_ok=True)

print(f"FRONTEND SMOKE TESTS PASSED: {len(checks) + 2} checks")
