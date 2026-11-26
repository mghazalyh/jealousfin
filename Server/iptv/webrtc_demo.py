"""Standalone no-login WebRTC video diagnostic. No production API or file access."""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
from pathlib import Path
from aiohttp import web
import webrtc_bridge as bridge

DEMO_PORT = 18802  # Virtual RPC target, not an HTTP listener.
LOG = logging.getLogger("jealouswin.rtc.demo")

class DemoPeer(bridge.Peer):
    async def handle(self, raw):
        rid = None
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                return
            rid = d.get("id")
            if type(rid) is not int or not 0 < rid < 2**32:
                return
            if d.get("port") != DEMO_PORT or d.get("method") != "GET" or d.get("body") is not None:
                return await self.respond(rid,403)
            target = d.get("path")
            if target == "/System/Info/Public":
                data = b'{"Id":"rtc-demo","ServerName":"Generated WebRTC video"}'
                return await self.respond(rid,200,data,{"content-type":"application/json"})
            if not isinstance(target,str):
                return await self.respond(rid,400)
            match = re.fullmatch(r"/media/fixture/(index\.m3u8|segment[0-9]{3}\.ts)",target)
            if not match:
                return await self.respond(rid,403)
            name = match.group(1)
            data = self.app["demo_media"].get(name)
            if data is None:
                return await self.respond(rid,404)
            mime = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
            headers = {"content-type":mime}
            status = 200
            request_headers = d.get("headers") or {}
            if not isinstance(request_headers,dict):
                return await self.respond(rid,400)
            byte_range = next((v for k,v in request_headers.items() if k.lower()=="range"),None)
            if byte_range is not None:
                m = re.fullmatch(r"bytes=([0-9]{1,8})-([0-9]{0,8})",byte_range) if isinstance(byte_range,str) else None
                if not m:
                    return await self.respond(rid,416)
                start,end = int(m.group(1)),int(m.group(2)) if m.group(2) else len(data)-1
                if start >= len(data) or end < start:
                    return await self.respond(rid,416)
                end = min(end,len(data)-1)
                headers.update({"content-range":f"bytes {start}-{end}/{len(data)}","accept-ranges":"bytes"})
                data,status = data[start:end+1],206
            LOG.info("demo media=%s status=%s bytes=%s",name,status,len(data))
            await self.respond(rid,status,data,headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("demo error=%s",type(exc).__name__)
            if type(rid) is int:
                try: await self.respond(rid,502)
                except Exception: await self.close()

def load_media(root):
    files = {}
    playlist = root / "index.m3u8"
    data = playlist.read_bytes()
    if len(data) > 65536:
        raise ValueError("demo playlist too large")
    files["index.m3u8"] = data
    names = [x.strip() for x in data.decode("utf-8").splitlines() if x.strip() and not x.startswith("#")]
    if not 1 <= len(names) <= 16 or any(not re.fullmatch(r"segment[0-9]{3}\.ts",name) for name in names):
        raise ValueError("invalid demo playlist")
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > bridge.MAX_BODY:
            raise ValueError("invalid demo segment")
        files[name] = path.read_bytes()
    return files

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind",required=True)
    parser.add_argument("--network",required=True)
    parser.add_argument("--media",required=True,type=Path)
    args = parser.parse_args()
    network = ipaddress.ip_network(args.network)
    address = ipaddress.ip_address(args.bind)
    if address.version != 4 or address not in network or not network.is_private or address.is_unspecified:
        parser.error("private LAN address required")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    for name in ("aioice","aiortc","aiohttp.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # This separate process has no production RPC implementation exposed.
    # The inherited transport supplies only framing, signaling and lifecycle.
    bridge.Peer = DemoPeer
    bridge.PAIR_HTML = bridge.PAIR_HTML.replace("your local television gateway","the generated test-video service")
    app = bridge.create_app(args.bind,18801,network)
    app["demo_media"] = load_media(args.media)
    web.run_app(app,host=args.bind,port=18801,access_log=None)

if __name__=="__main__":
    main()
