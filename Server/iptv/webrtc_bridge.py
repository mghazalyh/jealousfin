"""LAN-only WebRTC RPC bridge. No STUN, TURN, external signaling, or provider credentials."""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
import secrets
import struct
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

PUBLIC = "https://jealouswin.pages.dev"
PORTS = {18796, 18797}
MAX_BODY = 8 * 1024 * 1024
CHUNK = 16 * 1024
MAX_PEERS = 8
ID = re.compile(r"^[a-f0-9]{32}$")
LOG = logging.getLogger("jealouswin.rtc")

PAIR_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Jealouswin LAN pairing</title>
<style>body{background:#111318;color:#ddd;font:17px system-ui;padding:24px;max-width:520px;margin:auto}button{font:inherit;padding:15px;border-radius:10px;background:#a8c9eb;color:#102131;border:0}p{line-height:1.5}</style></head>
<body><h1>Connect Jealouswin</h1><p>Allow this Cloudflare-hosted Jealouswin tab to connect to your local television gateway using WebRTC. No login credentials are exchanged during pairing.</p>
<button id="approve" disabled>Approve LAN connection</button><p id="status">Waiting for Jealouswin…</p>
<script nonce="__CSP__">
'use strict';
const origin='https://jealouswin.pages.dev', nonce='__PAIR__';
const button=document.getElementById('approve'), status=document.getElementById('status');
let offer=null,busy=false;
const ready=()=>{if(window.opener)window.opener.postMessage({type:'jw-rtc-ready'},origin);else status.textContent='Open this tab from the public Jealouswin website.';};
const timer=setInterval(ready,1000);ready();
window.addEventListener('message',e=>{
 if(e.origin!==origin||e.source!==window.opener||e.data?.type!=='jw-rtc-offer'||busy)return;
 offer=e.data;button.disabled=false;status.textContent='Ready. Approve the connection to continue.';
});
button.onclick=async()=>{
 if(!offer||busy)return;busy=true;button.disabled=true;clearInterval(timer);
 status.textContent='Pairing…';
 try{
  const res=await fetch('/offer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce,sdp:offer.sdp})});
  if(!res.ok)throw new Error('Pairing rejected (HTTP '+res.status+')');
  const answer=await res.json();
  window.opener.postMessage({type:'jw-rtc-answer',id:offer.id,sdp:answer.sdp},origin);
  status.textContent='Paired. Return to the Jealouswin tab. You may close this tab.';
  setTimeout(()=>window.close(),600);
 }catch(e){status.textContent='Pairing failed. Return to Jealouswin and try again.';}
};
</script></body></html>"""

def route(path, method):
    if method == "GET" and path in ("/System/Info/Public", "/LiveTv/Channels", "/LiveTv/Programs", "/Jealousfin/Policy", "/LiveTv/Recordings"):
        return "api"
    if method == "POST" and path == "/Users/AuthenticateByName":
        return "login"
    if method == "GET" and re.fullmatch(r"/(?:malay/)?media/[^/]+/[^/]+", path):
        return "media"
    if method == "POST" and (path in ("/Sessions/Playing", "/Sessions/Playing/Progress", "/Sessions/Playing/Stopped", "/LiveStreams/Close") or re.fullmatch(r"/Items/[a-f0-9]{32}/PlaybackInfo", path)):
        return "api"
    if method in ("POST", "DELETE") and re.fullmatch(r"/Users/[^/]+/FavoriteItems/[a-f0-9]{32}", path):
        return "api"
    return None

def valid_offer(sdp, network):
    if not isinstance(sdp, str) or len(sdp) > 65536 or not sdp.startswith("v=0"):
        return False
    media = [x for x in sdp.splitlines() if x.startswith("m=")]
    if len(media) != 1 or not media[0].startswith("m=application "):
        return False
    for line in sdp.splitlines():
        if not line.startswith("a=candidate:"):
            continue
        parts = line.split()
        if len(parts) < 8 or parts[2].lower() != "udp" or parts[6:8] != ["typ", "host"]:
            return False
        address = parts[4]
        if re.fullmatch(r"[a-fA-F0-9-]{36}\.local", address):
            continue
        try:
            if ipaddress.ip_address(address) not in network:
                return False
        except ValueError:
            return False
    return True

def answer_lan_only(sdp, bind):
    return "\r\n".join(line for line in sdp.splitlines()
        if not line.startswith("a=candidate:") or (len(line.split()) >= 8 and line.split()[4] == bind and line.split()[6:8] == ["typ", "host"])) + "\r\n"

class Peer:
    def __init__(self, app, pc):
        self.app, self.pc = app, pc
        self.channel = None
        self.auth = set()
        self.tasks = set()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.last = time.monotonic()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in tuple(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        self.app["peers"].discard(self)
        await self.pc.close()

    def attach(self, channel):
        if self.channel is not None or channel.label != "jealouswin-rpc" or not channel.ordered:
            channel.close()
            return
        self.channel = channel
        @channel.on("message")
        def receive(message):
            self.last = time.monotonic()
            if not isinstance(message, str) or len(message) > 262144 or len(self.tasks) >= 12:
                return
            task = asyncio.create_task(self.handle(message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        @channel.on("close")
        def closed():
            asyncio.create_task(self.close())

    async def respond(self, rid, status, body=b"", headers=None):
        ch = self.channel
        async with self.send_lock:
            if self.closed or ch.readyState != "open":
                return
            ch.send(json.dumps({"id": rid, "status": status, "length": len(body), "headers": headers or {}}, separators=(",", ":")))
            for offset in range(0, len(body), CHUNK):
                deadline = time.monotonic() + 12
                while ch.bufferedAmount > 256 * 1024:
                    if self.closed or ch.readyState != "open" or time.monotonic() > deadline:
                        raise ConnectionError("channel backpressure")
                    await asyncio.sleep(.01)
                ch.send(struct.pack("!I", rid) + body[offset:offset + CHUNK])

    async def handle(self, raw):
        rid = None
        kind = None
        try:
            d = json.loads(raw)
            rid = d.get("id")
            if type(rid) is not int or not 0 < rid < 2**32:
                return
            port, method, target = d.get("port"), d.get("method"), d.get("path")
            if type(port) is not int or port not in PORTS or not isinstance(target, str) or len(target) > 8192:
                return await self.respond(rid, 400)
            u = urlsplit(target)
            if u.scheme or u.netloc or u.fragment or not u.path.startswith("/") or "\\" in u.path or "%" in u.path or ".." in u.path:
                return await self.respond(rid, 400)
            kind = route(u.path, method)
            if kind is None:
                return await self.respond(rid, 403)
            if kind != "login" and u.path != "/System/Info/Public" and port not in self.auth:
                return await self.respond(rid, 401)
            headers = {}
            allowed = {"accept", "content-type", "range", "x-emby-token", "x-emby-authorization", "x-jealousfin-request-id"}
            for k, v in (d.get("headers") or {}).items():
                if isinstance(k, str) and k.lower() in allowed and isinstance(v, str) and len(v) <= 4096 and "\r" not in v and "\n" not in v:
                    headers[k] = v
            body = d.get("body")
            if body is not None and (not isinstance(body, str) or len(body) > 262144):
                return await self.respond(rid, 413)
            if method != "POST" and body is not None:
                return await self.respond(rid, 400)
            async with self.app["http"].request(method, f"http://{self.app['bind']}:{port}{target}", headers=headers, data=body, allow_redirects=False) as response:
                result = bytearray()
                async for block in response.content.iter_chunked(65536):
                    result.extend(block)
                    if len(result) > MAX_BODY:
                        return await self.respond(rid, 502)
                status = response.status
                if kind == "login":
                    if status == 200:
                        self.auth.add(port)
                    else:
                        self.auth.discard(port)
                safe_headers = {k.lower():v for k,v in response.headers.items() if k.lower() in ("content-type","content-range","accept-ranges")}
                LOG.info("rpc port=%s route=%s method=%s status=%s bytes=%s",port,kind,method,status,len(result))
                await self.respond(rid, status, bytes(result), safe_headers)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.warning("rpc failure route=%s type=%s",kind,type(e).__name__)
            if rid is not None:
                try:
                    await self.respond(rid, 502)
                except Exception:
                    await self.close()

def local_client(request):
    try:
        return ipaddress.ip_address(request.remote) in request.app["network"]
    except (ValueError, TypeError):
        return False

async def pair(request):
    if not local_client(request):
        raise web.HTTPForbidden()
    now = time.monotonic()
    tokens = request.app["tokens"]
    for token, entry in list(tokens.items()):
        if entry[1] < now:
            tokens.pop(token, None)
    if len(tokens) >= 64:
        raise web.HTTPTooManyRequests()
    token, csp = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
    tokens[token] = (request.remote, now + 180)
    return web.Response(text=PAIR_HTML.replace("__CSP__", csp).replace("__PAIR__", token),content_type="text/html",headers={
        "Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
        "X-Frame-Options":"DENY","Content-Security-Policy":f"default-src 'none'; script-src 'nonce-{csp}'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

async def offer(request):
    app = request.app
    if not local_client(request) or request.headers.get("Origin") != app["origin"] or request.content_type != "application/json":
        raise web.HTTPForbidden()
    d = await request.json()
    if not isinstance(d, dict):
        raise web.HTTPBadRequest()
    entry = app["tokens"].pop(d.get("nonce",""),None)
    if not entry or entry[0] != request.remote or entry[1] < time.monotonic():
        raise web.HTTPForbidden()
    if len(app["peers"]) >= MAX_PEERS:
        raise web.HTTPTooManyRequests()
    sdp = d.get("sdp")
    if not valid_offer(sdp, app["network"]):
        raise web.HTTPBadRequest()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer = Peer(app, pc)
    app["peers"].add(peer)
    @pc.on("datachannel")
    def datachannel(ch):
        peer.attach(ch)
    @pc.on("connectionstatechange")
    async def state():
        LOG.info("peer state=%s",pc.connectionState)
        if pc.connectionState in ("failed","closed"):
            await peer.close()
    try:
        await asyncio.wait_for(pc.setRemoteDescription(RTCSessionDescription(sdp=sdp,type="offer")), 10)
        await asyncio.wait_for(pc.setLocalDescription(await pc.createAnswer()), 10)
        answer = answer_lan_only(pc.localDescription.sdp,app["bind"])
        if "a=candidate:" not in answer:
            raise RuntimeError("no LAN ICE candidate")
        return web.json_response({"sdp":answer},headers={"Cache-Control":"no-store"})
    except Exception as exc:
        LOG.warning("pair failed type=%s",type(exc).__name__)
        await peer.close()
        raise web.HTTPBadRequest()

async def lifecycle(app):
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45),cookie_jar=aiohttp.DummyCookieJar(),trust_env=False)
    async def sweep():
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for peer in list(app["peers"]):
                if (peer.pc.connectionState != "connected" and now-peer.last > 90) or now-peer.last > 900:
                    await peer.close()
    task = asyncio.create_task(sweep())
    yield
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    await asyncio.gather(*(p.close() for p in list(app["peers"])),return_exceptions=True)
    await app["http"].close()

def create_app(bind, port, network):
    app = web.Application(client_max_size=96*1024)
    app["bind"], app["network"], app["origin"] = bind, network, f"http://{bind}:{port}"
    app["peers"], app["tokens"] = set(), {}
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/pair",pair)
    app.router.add_post("/offer",offer)
    return app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind",required=True)
    parser.add_argument("--network",required=True)
    parser.add_argument("--port",type=int,default=18800)
    args = parser.parse_args()
    network = ipaddress.ip_network(args.network)
    address = ipaddress.ip_address(args.bind)
    if address.version != 4 or address not in network or not network.is_private or address.is_unspecified:
        parser.error("A private IPv4 LAN bind address and subnet are required")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    for name in ("aioice","aiortc","aiohttp.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    web.run_app(create_app(args.bind,args.port,network),host=args.bind,port=args.port,access_log=None)
