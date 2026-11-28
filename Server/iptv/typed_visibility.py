from __future__ import annotations

import concurrent.futures
import ipaddress
import http.client
import ssl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_HTML = 2 * 1024 * 1024
MAX_CSS = 2 * 1024 * 1024
MAX_CSS_TOTAL = 4 * 1024 * 1024
MAX_STYLESHEETS = 24
CSS_FETCH_WORKERS = 4
MAX_NODES = 20000
MAX_PAGE_SECONDS = 12.0
# Only CSS for these reviewed article profiles; no arbitrary third-party hosts.
CSS_HOSTS = {
    "amblin.com": {"amblin.com", "use.fontawesome.com", "cdn.fonts.net", "fonts.googleapis.com"},
    "theguardian.com": {"theguardian.com", "assets.guim.co.uk"},
    "screamhorrormag.com": {"screamhorrormag.com"},
    "indycar.com": {"indycar.com", "use.typekit.net", "p.typekit.net"},
    "theapex.racing": {"theapex.racing", "fonts.googleapis.com", "theissue.fuelthemes.net"},
    "racingnews.co": {"racingnews.co", "a.pub.network"},
    "thenewdispatch.com": {"thenewdispatch.com", "fonts.googleapis.com"},
}

def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")

def _import(value):
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    return bool(re.search(r"@(?:import\b|\\)", value, re.I))

def _links(soup):
    return [node for node in soup.find_all("link") if "stylesheet" in [str(x).lower() for x in node.get("rel", [])]]

def needs_resolution(page):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    return bool(_links(soup) or any(_import(s.get_text()) for s in soup.find_all("style")))

def _checked(url, hosts):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None,443) or p.fragment or len(url)>2048 or _host(url) not in hosts:
        raise ValueError("stylesheet URL not approved")
    addresses = socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("non-public stylesheet host")
    return p, addresses


def fetch_https(url, hosts, timeout, *, accept, content_types, max_bytes, validate_url=None):
    # Connect to the exact vetted sockaddr, never re-resolve the hostname.
    # HTTPSConnection still supplies the original hostname for TLS SNI and
    # certificate validation. Each redirect receives a fresh vetted address.
    origin = _host(url)
    deadline = time.monotonic() + timeout
    for _ in range(5):
        if validate_url is not None and not validate_url(url):
            raise ValueError("unapproved evidence URL")
        if _host(url) != origin:
            raise ValueError("cross-host evidence redirect")
        p, addresses = _checked(url, hosts)
        def connect_address(_address, timeout=None, source_address=None):
            for family, kind, proto, _, sockaddr in addresses:
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise TimeoutError("stylesheet deadline")
                sock=socket.socket(family,kind,proto)
                try:
                    sock.settimeout(remaining)
                    sock.connect(sockaddr)
                    if not ipaddress.ip_address(sock.getpeername()[0]).is_global:
                        raise ValueError("non-public stylesheet peer")
                    return sock
                except OSError:
                    sock.close()
                except BaseException:
                    sock.close()
                    raise
            raise OSError("no vetted stylesheet address reachable")
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError("stylesheet deadline")
        conn=http.client.HTTPSConnection(p.hostname,443,timeout=remaining,context=ssl.create_default_context())
        conn._create_connection=connect_address
        try:
            path=urllib.parse.urlunsplit(("","",p.path or "/",p.query,""))
            conn.request("GET",path,headers={"User-Agent":"Jealousfin-Episode-Evidence/1.0","Accept":accept,"Accept-Encoding":"identity"})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                target=response.getheader("Location")
                if not target:
                    raise ValueError("missing stylesheet redirect")
                url=urllib.parse.urljoin(url,target)
                continue
            if response.status!=200:
                raise urllib.error.HTTPError(url,response.status,response.reason,response.headers,None)
            if response.headers.get_content_type() not in content_types:
                raise ValueError("unexpected evidence content type")
            data=response.read(max_bytes+1)
            if len(data)>max_bytes:
                raise ValueError("evidence too large")
            return data
        finally:
            conn.close()
    raise ValueError("too many evidence redirects")


def _fetch_css(url, hosts, timeout):
    return fetch_https(url,hosts,timeout,accept="text/css",content_types={"text/css"},max_bytes=MAX_CSS)


def _render(page,url,seconds):
    from bs4 import BeautifulSoup
    from playwright.sync_api import sync_playwright
    host=_host(url)
    allowed=CSS_HOSTS.get(host)
    if not allowed or len(page.encode())>MAX_HTML:
        return None
    deadline=time.monotonic()+seconds
    soup=BeautifulSoup(page,"lxml")
    # CSS only: no page scripts, frames, refresh, forms, plugins or unrelated requests.
    for node in soup.find_all(("script","iframe","frame","object","embed","base","meta")):
        if node.name!="meta" or node.get("http-equiv"):
            node.decompose()
    for link in soup.find_all("link"):
        if "stylesheet" not in [str(x).lower() for x in link.get("rel", [])]:
            link.decompose()
    nodes=soup.find_all(True)
    if len(nodes)>MAX_NODES:
        return None
    marker="data-jf-"+uuid.uuid4().hex
    for i,node in enumerate(nodes):
        node[marker]=str(i)
        for key in list(node.attrs):
            if key.lower().startswith("on"):
                node.attrs.pop(key,None)
    base=soup.new_tag("base",href=url)
    if soup.head:
        soup.head.insert(0,base)
    else:
        soup.insert(0,base)
    failed=[]
    futures={}
    total=0
    totals_lock=threading.Lock()
    def remaining():
        return max(0.0,deadline-time.monotonic())
    def fetch_one(target):
        nonlocal total
        if remaining()<=0:
            raise ValueError("stylesheet deadline")
        body=_fetch_css(target,allowed,min(2.0,remaining()))
        with totals_lock:
            if total+len(body)>MAX_CSS_TOTAL:
                # Never retain a partial result from an over-budget sheet.
                raise ValueError("stylesheet total")
            total+=len(body)
        return body
    with concurrent.futures.ThreadPoolExecutor(max_workers=CSS_FETCH_WORKERS) as executor:
        def submit(target):
            if remaining()<=0 or _host(target) not in allowed:
                raise ValueError("unapproved stylesheet")
            if target not in futures:
                if len(futures)>=MAX_STYLESHEETS:
                    raise ValueError("stylesheet count")
                futures[target]=executor.submit(fetch_one,target)
            return futures[target]
        # The browser still determines which sheets/imports it needs and applies
        # CSS in document order. Start the reviewed direct links concurrently;
        # imports are submitted only when requested. No page scripts execute.
        for link in _links(soup):
            target=urllib.parse.urljoin(url,str(link.get("href") or ""))
            try:
                submit(target)
            except (ValueError,TypeError):
                # An unused or disabled stylesheet need not be fetched. If
                # Chromium requests it, the same admission check rejects it.
                pass
        def route_request(route):
            req=route.request
            if req.resource_type!="stylesheet":
                route.abort()
                return
            try:
                body=submit(req.url).result(timeout=remaining())
                if remaining()<=0:
                    raise ValueError("stylesheet deadline")
                route.fulfill(status=200,content_type="text/css; charset=utf-8",body=body)
            except Exception:
                failed.append(True)
                route.abort()
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,chromium_sandbox=True,timeout=max(1,int(remaining()*1000)),args=["--disable-background-networking","--dns-prefetch-disable","--disable-preconnect"])
            try:
                ctx=browser.new_context(java_script_enabled=False,service_workers="block",viewport={"width":1280,"height":900})
                ctx.route("**/*",route_request)
                tab=ctx.new_page()
                tab.set_content(str(soup),wait_until="load",timeout=max(1,int(remaining()*1000)))
                if failed or remaining()<=0:
                    return None
                if not tab.evaluate("() => Array.from(document.querySelectorAll('link[rel~=stylesheet]')).every(n => !!n.sheet)"):
                    return None
                # Browser evaluation is trusted local code; page JavaScript stays disabled.
                hidden=tab.evaluate(r"""marker => Array.from(document.querySelectorAll('['+marker+']')).filter(n=>{
                    const s=getComputedStyle(n), r=n.getBoundingClientRect();
                    if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)<=0||s.contentVisibility==='hidden'||parseFloat(s.fontSize)===0) return true;
                    if(s.color==='transparent'||s.webkitTextFillColor==='transparent'||/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(s.color)||/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(s.webkitTextFillColor)) return true;
                    // A normal article may continue below the fold. Explicitly
                    // positioned/transformed boxes outside the viewport are not
                    // reliable source evidence, unlike ordinary document flow.
                    if(Math.abs(parseFloat(s.textIndent))>=1000) return true;
                    const positioned=s.position!=='static', outOfFlow=s.position==='absolute'||s.position==='fixed', transformed=s.transform!=='none'||s.translate!=='none'||s.scale!=='none'||s.rotate!=='none';
                    if(positioned&&[s.left,s.right,s.top,s.bottom].some(v=>Math.abs(parseFloat(v))>=1000)) return true;
                    if((positioned||transformed)&&(r.right<=0||r.left>=innerWidth||r.bottom<=0)) return true;
                    if((outOfFlow||transformed)&&r.top>=innerHeight) return true;
                    if(transformed){
                        if(r.width<=0||r.height<=0) return true;
                        if(s.transform!=='none'){
                            const m=new DOMMatrixReadOnly(s.transform);
                            if(Math.abs(m.m41)>=1000||Math.abs(m.m42)>=1000||Math.abs(m.m43)>=1000) return true;
                        }
                        if(s.translate!=='none'&&s.translate.split(/\s+/).some(v=>Math.abs(parseFloat(v))>=1000)) return true;
                    }
                    // Exclude actual text boxes entirely outside the horizontal
                    // viewing area even if the parent retains a visible box.
                    if(n.childNodes.length&&Array.from(n.childNodes).some(t=>t.nodeType===3&&t.textContent.trim())){
                        const ranges=Array.from(n.childNodes).filter(t=>t.nodeType===3&&t.textContent.trim()).flatMap(t=>{const q=document.createRange();q.selectNode(t);return Array.from(q.getClientRects());});
                        if(ranges.length&&ranges.every(q=>q.right<=0||q.left>=innerWidth||q.bottom<=0||q.width<=0||q.height<=0)) return true;
                    }
                    if(s.display!=='contents'&&(r.width<=0||r.height<=0)&&/hidden|clip/.test(s.overflow)) return true;
                    if(s.clipPath!=='none'||(s.clip!=='auto'&&s.clip!=='rect(auto, auto, auto, auto)')) return true;
                    if(s.maskImage&&s.maskImage!=='none') return true;
                    if(s.filter&&/opacity\(0(?:%|\.0+)?\)/.test(s.filter)) return true;
                    return false;
                }).map(n=>Number(n.getAttribute(marker)))""",marker)
                if failed or remaining()<=0 or not isinstance(hidden,list):
                    return None
            finally:
                browser.close()
    for i in hidden:
        if type(i) is not int or not 0<=i<len(nodes):
            return None
        nodes[i]["hidden"]=""
    for node in nodes:
        node.attrs.pop(marker,None)
    base.decompose()
    for node in _links(soup):
        node.decompose()
    for node in soup.find_all("style"):
        if _import(node.get_text()):
            node.decompose()
    result=str(soup)
    return result if len(result.encode())<=MAX_HTML else None

def resolve(page,url,deadline):
    """Resolve external styles in a hard-bounded, isolated child; no source pages on disk."""
    if not isinstance(page,(bytes,str)):
        return None
    if isinstance(page,bytes):
        page=page.decode("utf-8","replace")
    if len(page.encode())>MAX_HTML or _host(url) not in CSS_HOSTS:
        return None
    seconds=min(MAX_PAGE_SECONDS,deadline-time.monotonic())
    if seconds<1:
        return None
    env={k:v for k,v in os.environ.items() if k in ("HOME","USER","LOGNAME","PATH","LANG","LC_ALL","TMPDIR","SSL_CERT_FILE","SSL_CERT_DIR","PLAYWRIGHT_BROWSERS_PATH")}
    payload=json.dumps({"page":page,"url":url,"seconds":max(0.5,seconds-0.5)}).encode()
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
    try:
        out,_=proc.communicate(payload,timeout=seconds)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.communicate()
        return None
    if proc.returncode or len(out)>MAX_HTML:
        return None
    return out if out else None

if __name__=="__main__":
    try:
        value=json.load(sys.stdin)
        result=_render(value["page"],value["url"],float(value["seconds"]))
        if result is not None:
            sys.stdout.write(result)
    except Exception:
        sys.exit(1)
