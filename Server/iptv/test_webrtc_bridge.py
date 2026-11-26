"""Local WebRTC fixture acceptance, independent of live credentials or providers."""
import asyncio
import ipaddress
import json
import re
import struct
import unittest
from unittest.mock import patch

try:
    import aiohttp
    from aiohttp import web
    import aioice.ice
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    import webrtc_bridge as bridge
except ImportError as exc:
    raise unittest.SkipTest("WebRTC bridge dependencies are not installed") from exc

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_rpc_authentication_and_binary_segments(self):
        candidates=aioice.ice.get_host_addresses(use_ipv4=True,use_ipv6=False)
        bind=next((a for a in candidates if ipaddress.ip_address(a).is_private),None)
        if not bind:
            self.skipTest("no private non-loopback ICE interface")
        payload = bytes(range(251)) * 5000
        async def fixture(request):
            if request.path == "/System/Info/Public":
                return web.json_response({"Id":"fixture","ServerName":"Fixture Gateway"})
            if request.path == "/Users/AuthenticateByName":
                d=await request.json()
                if d != {"Username":"fixture","Pw":"fixture-only"}:
                    return web.Response(status=401)
                return web.json_response({"AccessToken":"fixture-token","User":{"Id":"fixture"}})
            if request.path == "/LiveTv/Channels":
                if request.headers.get("X-Emby-Token") != "fixture-token":
                    return web.Response(status=401)
                return web.json_response({"Items":[]})
            if request.path == "/media/fixture-token/segment.ts":
                self.assertEqual(request.headers.get("Range"),"bytes=0-1254999")
                return web.Response(status=206,body=payload,headers={"Content-Range":"bytes 0-1254999/1255000"})
            return web.Response(status=404)
        fixture_app=web.Application()
        fixture_app.router.add_route("*","/{path:.*}",fixture)
        fixture_runner=web.AppRunner(fixture_app)
        await fixture_runner.setup()
        self.addAsyncCleanup(fixture_runner.cleanup)
        fixture_site=web.TCPSite(fixture_runner,bind,0)
        await fixture_site.start()
        port=fixture_site._server.sockets[0].getsockname()[1]
        with patch.object(bridge,"PORTS",{port}):
            # Use a real LAN-capable host candidate for the peer transport,
            # while the test HTTP server uses an ephemeral loopback port.
            app=bridge.create_app(bind,18800,ipaddress.ip_network(bind+"/24",strict=False))
            runner=web.AppRunner(app)
            await runner.setup()
            self.addAsyncCleanup(runner.cleanup)
            site=web.TCPSite(runner,bind,0)
            await site.start()
            http_port=site._server.sockets[0].getsockname()[1]
            app["origin"]=f"http://{bind}:{http_port}"
            pc=RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.addAsyncCleanup(pc.close)
            ch=pc.createDataChannel("jealouswin-rpc",ordered=True)
            opened=asyncio.Event()
            responses={}
            chunks={}
            @ch.on("open")
            def onopen():
                opened.set()
            @ch.on("message")
            def message(data):
                if isinstance(data,str):
                    row=json.loads(data)
                    chunks[row["id"]]=(row,bytearray())
                    if row["length"]==0:
                        responses[row["id"]].set_result((row,b""))
                else:
                    rid=struct.unpack("!I",data[:4])[0]
                    row,body=chunks[rid]
                    body.extend(data[4:])
                    if len(body)==row["length"]:
                        responses[rid].set_result((row,bytes(body)))
            base=f"http://{bind}:{http_port}"
            await pc.setLocalDescription(await pc.createOffer())
            sdp=bridge.answer_lan_only(pc.localDescription.sdp,bind)
            async with aiohttp.ClientSession() as session:
                async with session.get(base+"/pair") as r:
                    self.assertEqual(r.status,200)
                    nonce=re.search(r"nonce='([^']+)'",await r.text()).group(1)
                async with session.post(base+"/offer",json={"nonce":nonce,"sdp":sdp},headers={"Origin":"https://unapproved.invalid"}) as r:
                    self.assertEqual(r.status,403)
                async with session.post(base+"/offer",json={"nonce":nonce,"sdp":sdp},headers={"Origin":base}) as r:
                    self.assertEqual(r.status,200)
                    answer=await r.json()
                async with session.post(base+"/offer",json={"nonce":nonce,"sdp":sdp},headers={"Origin":base}) as r:
                    self.assertEqual(r.status,403)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"],type="answer"))
            await asyncio.wait_for(opened.wait(),10)
            async def rpc(rid,path,method="GET",headers=None,body=None):
                responses[rid]=asyncio.get_running_loop().create_future()
                ch.send(json.dumps({"id":rid,"port":port,"method":method,"path":path,"headers":headers or {},"body":body}))
                return await asyncio.wait_for(responses[rid],15)
            self.assertEqual((await rpc(1,"/System/Info/Public"))[0]["status"],200)
            self.assertEqual((await rpc(2,"/LiveTv/Channels"))[0]["status"],401)
            self.assertEqual((await rpc(3,"/Users/AuthenticateByName","POST",{"Content-Type":"application/json"},'{"Username":"fixture","Pw":"wrong"}'))[0]["status"],401)
            self.assertEqual((await rpc(4,"/Users/AuthenticateByName","POST",{"Content-Type":"application/json"},'{"Username":"fixture","Pw":"fixture-only"}'))[0]["status"],200)
            self.assertEqual((await rpc(5,"/LiveTv/Channels",headers={"X-Emby-Token":"fixture-token"}))[0]["status"],200)
            row,data=await rpc(6,"/media/fixture-token/segment.ts",headers={"Range":"bytes=0-1254999"})
            self.assertEqual(row["status"],206)
            self.assertEqual(data,payload)
            self.assertEqual((await rpc(7,"/gateway-runtime.json"))[0]["status"],403)
            self.assertEqual((await rpc(8,"http://example.com/"))[0]["status"],400)

    def test_offer_rejects_external_and_non_data_candidates(self):
        network=ipaddress.ip_network("192.168.31.0/24")
        base="v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        self.assertTrue(bridge.valid_offer(base+"a=candidate:1 1 udp 1 192.168.31.209 12345 typ host\r\n",network))
        self.assertFalse(bridge.valid_offer(base+"a=candidate:1 1 udp 1 8.8.8.8 12345 typ host\r\n",network))
        self.assertFalse(bridge.valid_offer(base+"m=video 9 UDP/TLS/RTP/SAVPF 96\r\n",network))

if __name__=="__main__":
    unittest.main()
