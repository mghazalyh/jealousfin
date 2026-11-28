from __future__ import annotations

import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_HTML = 2 * 1024 * 1024
MAX_CSS = 2 * 1024 * 1024
MAX_CSS_TOTAL = 4 * 1024 * 1024
MAX_STYLESHEETS = 24
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
    return url

def _fetch_css(url, hosts, timeout):
    origin = _host(url)
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            target = urllib.parse.urljoin(req.full_url,newurl)
            if _host(target)!=origin:
                raise ValueError("cross-host stylesheet redirect")
            return super().redirect_request(req,fp,code,msg,headers,_checked(target,hosts))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect())
    request = urllib.request.Request(_checked(url,hosts),headers={"User-Agent":"Jealousfin-Episode-Evidence/1.0","Accept":"text/css"})
    with opener.open(request,timeout=timeout) as response:
        if response.headers.get_content_type() != "text/css":
            raise ValueError("not CSS")
        data=response.read(MAX_CSS+1)
        if len(data)>MAX_CSS:
            raise ValueError("stylesheet too large")
        return data

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
    cache={}
    total=0
    def remaining():
        return max(0.0,deadline-time.monotonic())
    def route_request(route):
        nonlocal total
        req=route.request
        if req.resource_type!="stylesheet":
            route.abort()
            return
        try:
            if remaining()<=0 or _host(req.url) not in allowed:
                raise ValueError("unapproved stylesheet")
            if req.url not in cache:
                if len(cache)>=MAX_STYLESHEETS:
                    raise ValueError("stylesheet count")
                body=_fetch_css(req.url,allowed,min(2.0,remaining()))
                total+=len(body)
                if total>MAX_CSS_TOTAL:
                    raise ValueError("stylesheet total")
                cache[req.url]=body
            route.fulfill(status=200,content_type="text/css; charset=utf-8",body=cache[req.url])
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
                const positioned=s.position!=='static', transformed=s.transform!=='none'||s.translate!=='none'||s.scale!=='none'||s.rotate!=='none';
                if(positioned&&[s.left,s.right,s.top,s.bottom].some(v=>Math.abs(parseFloat(v))>=1000)) return true;
                if((positioned||transformed)&&(r.right<=0||r.left>=innerWidth||r.bottom<=0||r.top>=innerHeight)) return true;
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
