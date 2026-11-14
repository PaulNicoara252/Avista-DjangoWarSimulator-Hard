import json
import requests

HOST = "https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net"
CANDIDATES = [
    "/draft?batch=0", "/drafts?batch=0", "/drafting?batch=0",
    "/draft-people?batch=0", "/draft-soldiers?batch=0", "/draft-batch?batch=0",
    "/ranks?batch=0", "/rank?batch=0", "/batches?batch=0",
    "/fight?batch=0", "/fights?batch=0", "/fight-events?batch=0", "/events?batch=0",
]

for path in CANDIDATES:
    try:
        r = requests.get(HOST + path, timeout=15)
        if r.status_code != 404:
            print(f"=== {path} -> HTTP {r.status_code} ===")
            print(json.dumps(r.json(), indent=2)[:900])
    except Exception as e:
        print(f"=== {path} -> ERR: {e} ===")