# -*- coding: utf-8 -*-
"""Probe whether GitHub Actions can reach boatrace odds pages, and how.
Tries several client configurations and prints status/elapsed/odds-count for each."""
import time, urllib.request, urllib.error

URL = "https://www.boatrace.jp/owpc/pc/race/odds3t?rno=1&jcd=01&hd=20260614"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def count_odds(t): return t.count("oddsPoint")

def try_urllib(label, headers, timeout=15):
    req = urllib.request.Request(URL, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        print(f"[{label}] status={r.status} ms={int((time.time()-t0)*1000)} odds={count_odds(body)} bytes={len(body)}")
    except urllib.error.HTTPError as e:
        print(f"[{label}] HTTPError {e.code} ms={int((time.time()-t0)*1000)}")
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:120]} ms={int((time.time()-t0)*1000)}")

def try_requests(label, headers, timeout=15):
    try:
        import requests
    except Exception:
        print(f"[{label}] requests not installed"); return
    t0 = time.time()
    try:
        r = requests.get(URL, headers=headers, timeout=timeout)
        print(f"[{label}] status={r.status_code} ms={int((time.time()-t0)*1000)} odds={count_odds(r.text)} bytes={len(r.text)}")
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:120]} ms={int((time.time()-t0)*1000)}")

def try_httpx(label, http2):
    try:
        import httpx
    except Exception:
        print(f"[{label}] httpx not installed"); return
    t0 = time.time()
    try:
        with httpx.Client(http2=http2, timeout=15, headers={"User-Agent": UA_BROWSER}) as c:
            r = c.get(URL)
        print(f"[{label}] status={r.status_code} ms={int((time.time()-t0)*1000)} odds={count_odds(r.text)} http2={http2}")
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:120]} ms={int((time.time()-t0)*1000)}")

print("=== PROBE START ===")
try_urllib("urllib-min", {"User-Agent": "python-urllib"})
try_urllib("urllib-browserUA", {"User-Agent": UA_BROWSER,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9", "Accept-Encoding": "identity"})
try_requests("requests-min", {"User-Agent": "python-requests"})
try_requests("requests-browserUA", {"User-Agent": UA_BROWSER,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9"})
try_httpx("httpx-h1", False)
try_httpx("httpx-h2", True)
print("=== PROBE END ===")
