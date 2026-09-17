import json
import sys
import httpx
from common.config import settings

path = sys.argv[1] if len(sys.argv) > 1 else "/api/v1/cycles/latest"
if not path.startswith("/api/v1/") or "://" in path:
    raise SystemExit("Use a relative /api/v1/... path")
r = httpx.get(
    settings.signal_api_url + path,
    headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
    timeout=15,
)
print("HTTP", r.status_code)
print(json.dumps(r.json(), ensure_ascii=False, indent=2))
