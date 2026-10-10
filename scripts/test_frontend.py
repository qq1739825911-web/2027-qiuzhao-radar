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
    "search uses cached normalized text": "searchText" in source,
    "job detail text sanitization exists": "function sanitizeDetailField" in source,
    "lazy rendering of company groups exists": 'data-loaded="false"' in source and 'data-company-index="' in source,
    "review bucket fallback exists": 'const emptyBox=$("jobs").querySelector(".empty")' in source,
    "official recruitment route is distinguished": "function usesOfficialRecruitmentFlow" in source,
    "close button has accessible label": 'aria-label="关闭详情"' in source,
    "reset control exists": 'id="reset"' in source,
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

print(f"FRONTEND SMOKE TESTS PASSED: {len(checks) + 1} checks")
