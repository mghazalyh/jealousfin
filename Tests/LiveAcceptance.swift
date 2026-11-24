import Foundation
import AVFoundation
import CoreVideo

@main struct LiveAcceptance {
    @MainActor static func require(_ condition:Bool,_ stage:String) throws {
        guard condition else { throw AppError.message(stage) }
    }
    @MainActor static func observe(_ session:StreamSession) async -> Bool {
        let item=AVPlayerItem(url:session.url)
        let output=AVPlayerItemVideoOutput(pixelBufferAttributes:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA])
        item.add(output)
        let player=AVPlayer(playerItem:item)
        player.automaticallyWaitsToMinimizeStalling=true
        defer { player.pause();player.replaceCurrentItem(with:nil) }
        player.play()
        let deadline=Date().addingTimeInterval(45)
        var start:Double?;var frames=0;var latest = -Double.infinity
        while Date()<deadline {
            try? await Task.sleep(nanoseconds:500_000_000)
            if item.status == .failed {
                let error=item.error as NSError?
                print("AVPlayer failed code=\(error?.code ?? 0)")
                return false
            }
            let t=player.currentTime()
            if t.seconds.isFinite && player.timeControlStatus == .playing {
                if start==nil { start=t.seconds }
                var display=CMTime.invalid
                if output.hasNewPixelBuffer(forItemTime:t),output.copyPixelBuffer(forItemTime:t,itemTimeForDisplay:&display) != nil,display.seconds.isFinite,display.seconds>latest {
                    latest=display.seconds;frames+=1
                }
                if let initial=start,t.seconds-initial>=8,frames>=5 {
                    print("PASS AVPlayer advanced seconds=\(t.seconds-initial) decodedFrames=\(frames)")
                    return true
                }
            }
        }
        print("FAIL AVPlayer deadline decodedFrames=\(frames)")
        return false
    }
    @MainActor static func main() async {
        var stage="configuration"
        var failures=0
        let env=ProcessInfo.processInfo.environment
        var settings=Settings()
        settings.server=env["JELLYFIN_TEST_URL"] ?? ""
        settings.username=env["JELLYFIN_TEST_USERNAME"] ?? ""
        settings.password=env["JELLYFIN_TEST_PASSWORD"] ?? ""
        let client=Jellyfin(deviceID:"ci-"+UUID().uuidString,transport:URLSession(configuration:.ephemeral))
        do {
            try require(URL(string:settings.server)?.scheme=="https" && URL(string:settings.server)?.host=="desktop-hb9oshs.tail1fc8b.ts.net" && !settings.username.isEmpty && !settings.password.isEmpty,"missing/invalid configuration")
            stage="native authentication"
            try await client.connect(settings)
            try require(client.connected,"authentication")
            print("PASS native authentication")
            stage="native catalogue"
            let channels=try await client.channels()
            try require(!channels.isEmpty,"empty catalogue")
            print("PASS native catalogue count=\(channels.count)")
            stage="native health"
            let healthy=await client.healthy()
            try require(healthy,"health")
            print("PASS native health")
            stage="live API hot patch"
            let engine=ScriptEngine()
            let adapter=try await JellyfinAPIAdapter.load(JellyfinAdapterRevision(id:"ci-live",source:"var hooks={configure:function(p){p.timeoutSeconds=35;return p;},request:function(p){if(p.operation==='channels'){p.query.Limit='200';}return p;},response:function(p){return p.response;},stream:function(p){return p;}};"),engine:engine)
            client.adapter=adapter
            let patched=try await client.channels()
            try require(!patched.isEmpty && client.adapterID=="ci-live","patched catalogue")
            print("PASS live adapter catalogue")
            client.adapter=nil
            let modes=["All Channels","Movies & Series","Sports"]
            for mode in modes {
                stage="native server Surf "+mode
                guard let channel=channels.first(where:{$0.name.lowercased()=="surf: "+mode.lowercased()}) else {
                    failures+=1;print("FAIL "+stage+" missing channel");continue
                }
                do {
                    let session=try await client.stream(channel,mbps:settings.qualityMbps)
                    let ok=await observe(session)
                    await client.close(session,position:0)
                    if !ok { failures+=1 }
                    print((ok ? "PASS ":"FAIL ")+stage)
                } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
            }
            let eligible=channels.filter{!$0.isSurf}
            for mode in modes {
                stage="native app-owned Surf "+mode
                let candidates=eligible.filter{ c in
                    if mode=="Sports" {return c.genres.contains{$0.localizedCaseInsensitiveContains("sport")}}
                    if mode=="Movies & Series" {return c.genres.contains{$0.localizedCaseInsensitiveContains("movie") || $0.localizedCaseInsensitiveContains("series") || $0.localizedCaseInsensitiveContains("drama")}}
                    return true
                }
                guard let channel=candidates.first else {failures+=1;print("FAIL "+stage+" empty pool");continue}
                do {
                    let session=try await client.stream(channel,mbps:settings.qualityMbps)
                    let ok=await observe(session)
                    await client.close(session,position:0)
                    if !ok {failures+=1}
                    print((ok ? "PASS ":"FAIL ")+stage)
                } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
            }
        } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
        if client.connected {
            do {_ = try await client.request("Sessions/Logout",method:"POST");print("PASS logout")}
            catch {failures+=1;print("FAIL logout")}
        }
        print("LIVE_ACCEPTANCE_FAILURES=\(failures)")
        if failures>0 {exit(1)}
    }
}
