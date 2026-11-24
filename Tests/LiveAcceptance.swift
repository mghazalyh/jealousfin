import Foundation
import AVFoundation
import CoreVideo
import QuartzCore

@main struct LiveAcceptance {
    @MainActor static func require(_ condition:Bool,_ stage:String) throws {
        guard condition else { throw AppError.message(stage) }
    }
    @MainActor static func observe(_ session:StreamSession) async -> Bool {
        print("PROBE transport="+session.method)
        let item=AVPlayerItem(url:session.url)
        let output=AVPlayerItemVideoOutput(pixelBufferAttributes:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA])
        item.add(output)
        let player=AVPlayer(playerItem:item)
        player.automaticallyWaitsToMinimizeStalling=true
        player.isMuted=true
        defer { player.pause();player.replaceCurrentItem(with:nil) }
        player.play()
        let deadline=Date().addingTimeInterval(50)
        var start:Double?;var frames=0;var latest = -Double.infinity;var lastState=""
        while Date()<deadline {
            try? await Task.sleep(nanoseconds:500_000_000)
            if item.status == .failed {
                let error=item.error as NSError?
                print("AVPlayer failed code=\(error?.code ?? 0)")
                return false
            }
            let t=player.currentTime()
            let state="item=\(item.status.rawValue) control=\(player.timeControlStatus.rawValue) waiting=\(player.reasonForWaitingToPlay?.rawValue ?? "none")"
            if state != lastState {print("PLAYER_STATE "+state);lastState=state}
            if t.seconds.isFinite && player.timeControlStatus == .playing {
                if start==nil { start=t.seconds }
                var display=CMTime.invalid
                let hostTime=output.itemTime(forHostTime:CACurrentMediaTime())
                let sampleTime=hostTime.isValid && hostTime.seconds.isFinite ? hostTime:t
                if output.hasNewPixelBuffer(forItemTime:sampleTime),output.copyPixelBuffer(forItemTime:sampleTime,itemTimeForDisplay:&display) != nil,display.seconds.isFinite,display.seconds>latest {
                    latest=display.seconds;frames+=1
                }
                if let initial=start,t.seconds-initial>=8,frames>=5 {
                    print("PASS AVPlayer advanced seconds=\(t.seconds-initial) decodedFrames=\(frames)")
                    return true
                }
            }
        }
        let access=item.accessLog()?.events.last
        let errors=item.errorLog()?.events.map{$0.errorStatusCode} ?? []
        print("FAIL AVPlayer deadline decodedFrames=\(frames) position=\(player.currentTime().seconds) bytes=\(access?.numberOfBytesTransferred ?? 0) errorCodes=\(errors)")
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
        if let control=env["JEALOUSFIN_AVPLAYER_CONTROL"],FileManager.default.fileExists(atPath:control) {
            let session=StreamSession(url:URL(fileURLWithPath:control),itemID:"control",mediaSourceID:"control",playSessionID:"control",liveStreamID:nil,method:"LocalControl")
            let ok=await observe(session)
            if !ok {failures+=1}
            print((ok ? "PASS ":"FAIL ")+"hosted AVPlayer local H264 control")
        }else{failures+=1;print("FAIL hosted AVPlayer control fixture missing")}
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
            try require(client.sharedSurfAvailable,"shared Surf gateway unavailable")
            let modes=["All Channels","Movies & Series","Sports"]
            for mode in modes {
                stage="native server Surf "+mode
                guard let channel=client.sharedSurfChannel(category:mode,channels:channels) else {
                    failures+=1;print("FAIL "+stage+" missing channel");continue
                }
                do {
                    let session=try await client.stream(channel,mbps:settings.qualityMbps)
                    try require(session.isSharedSurf,"wrong native shared transport")
                    let ok=await observe(session)
                    await client.close(session,position:0)
                    if !ok { failures+=1 }
                    print((ok ? "PASS ":"FAIL ")+stage)
                } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
            }
            // Exercise the exact shared-category resolution used by AppModel.next,
            // including a fresh ticket on every reconnect and an active API patch.
            client.adapter=adapter
            for mode in modes {
                stage="native app-owned Surf "+mode
                guard let channel=client.sharedSurfChannel(category:mode,channels:patched) else {
                    failures+=1;print("FAIL "+stage+" missing shared category");continue
                }
                do {
                    let session=try await client.stream(channel,mbps:settings.qualityMbps)
                    try require(session.isSharedSurf,"shared category must not allocate a Jellyfin stream")
                    let ok=await observe(session)
                    await client.close(session,position:0)
                    if !ok {failures+=1}
                    print((ok ? "PASS ":"FAIL ")+stage)
                } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
            }
            client.adapter=nil
            stage="ordinary Jellyfin playback"
            var ordinaryOK=false
            for channel in channels.filter({!$0.isSurf}).prefix(3) {
                do {
                    let session=try await client.stream(channel,mbps:settings.qualityMbps)
                    let ok=await observe(session)
                    await client.close(session,position:0)
                    if ok {ordinaryOK=true;break}
                }catch{print("ordinary playback errorType="+String(describing:type(of:error)))}
            }
            if !ordinaryOK {failures+=1}
            print((ordinaryOK ? "PASS ":"FAIL ")+stage)
        } catch {failures+=1;print("FAIL "+stage+" errorType="+String(describing:type(of:error)))}
        if client.connected {
            do {_ = try await client.request("Sessions/Logout",method:"POST");print("PASS logout")}
            catch {failures+=1;print("FAIL logout")}
        }
        print("LIVE_ACCEPTANCE_FAILURES=\(failures)")
        if failures>0 {exit(1)}
    }
}
