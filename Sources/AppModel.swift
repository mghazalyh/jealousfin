import Foundation
import SwiftUI
import AVFoundation
import UIKit
import MediaPlayer

@MainActor final class AppModel: ObservableObject {
    @Published var settings:Settings
    @Published var channels:[Channel]=[]
    @Published var programs:[Program]=[]
    @Published var recordings:[String]=[]
    @Published var health:[String:ChannelHealth]=[:]
    @Published var events:[Event]=[]
    @Published var insights:[Insight]=[]
    @Published var status="Not connected"
    @Published var syncStatus="Not configured"
    @Published var lanStatus="Not started"
    @Published var current:Channel?
    @Published var previous:[Channel]=[]
    @Published var surfing=false
    @Published var surfPaused=false
    @Published var playing=false
    @Published var remaining=0
    @Published var ready=false
    @Published var metrics:[String:String]=[:]
    @Published var tab=0
    @Published var scriptOutput=""
    @Published var commandOutput=""
    let player=AVPlayer()
    let disk=Disk()
    let db=TorrioDB()
    let lan=LANServer()
    let scripts=ScriptEngine()
    let jelly:Jellyfin
    let deviceID:String
    private var ledger:HealthLedger
    private var session:StreamSession?
    private var work:Task<Void,Never>?
    private var supervisor:Task<Void,Never>?
    private var syncTask:Task<Void,Never>?
    private var generation=UUID()
    private var trace:String?
    private var nextRotation:Date?
    private var lastPosition:Double=0
    private var lastAdvance:Date?
    private var lastSuccess:Date?
    private var lastReport=Date.distantPast
    private var lastRefresh=Date.distantPast
    private var lastSync=Date.distantPast
    private var lastLogin=Date.distantPast
    private var lastHeartbeat=Date.distantPast
    private var videoOutput:AVPlayerItemVideoOutput?
    private var startUptime:TimeInterval=0
    private var startedSession=false
    private var validated=false
    private var failureRun=0
    private var attemptedIDs=Set<String>()
    private var pending=Set<String>()
    private var started=false
    private var loginBusy=false
    private var intentionallyPaused=false
    private var lastTick=Date()
    private var healthDirty=false
    private var pausedRotationSeconds:Int?
    private var emptyPoolRetry:Date?
    private var retryCurrentAfterResume=false
    private var catalogueBusy=false
    private var favoriteInFlight=Set<String>()
    private var lastProgressReport:Task<Void,Never>?
    private var cleanupTask:Task<Void,Never>?
    private var lastPlayerError:String?
    @Published private(set) var catalogueReady=false
    private var deviceObservers:[NSObjectProtocol]=[]

    init() {
        settings=disk.load(Settings.self,"settings.json") ?? Settings()
        ledger=disk.load(HealthLedger.self,"health.json") ?? HealthLedger()
        health=ledger.states()
        deviceID=disk.load(String.self,"device.json") ?? UUID().uuidString
        jelly=Jellyfin(deviceID:deviceID)
        disk.save(deviceID,"device.json")
        channels=disk.load([Channel].self,"channels.json") ?? []
        for url in disk.files(prefix:"event-").suffix(150) {
            if let d=try? Data(contentsOf:url),let e=try? JSONCodec.decoder().decode(Event.self,from:d){ events.append(e) }
        }
        jelly.log={ [weak self] severity,code,fields in self?.log(severity,"jellyfin",code,fields) }
        lan.handler={ [weak self] data,completion in
            Task { @MainActor in
                guard let self=self else { completion(Data("{}".utf8)); return }
                do {
                    let c=try JSONCodec.decoder().decode(Command.self,from:data)
                    let receipt=await self.execute(c)
                    completion(try JSONCodec.encoder().encode(receipt))
                } catch { completion(Data("{\"status\":\"failed\",\"result\":\"Invalid command\"}".utf8)) }
            }
        }
        lan.state={ [weak self] state in Task { @MainActor in self?.lanStatus=state; self?.log("INFO","lan","LISTENER_STATE",["state":state]) } }
        player.automaticallyWaitsToMinimizeStalling=true
        UIDevice.current.isBatteryMonitoringEnabled=true
        recalculateInsights()
    }
    func start() {
        guard !started else {return}; started=true
        log("INFO","app","START",["version":Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown","os":UIDevice.current.systemVersion])
        configureAudio()
        observeLifecycle()
        configureLAN()
        if settings.autoLogin && !settings.server.isEmpty { Task { await connect() } }
        supervisor=Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds:1_000_000_000)
                guard !Task.isCancelled,let self=self else {return}
                await self.tick()
            }
        }
    }
    func saveSettings() {
        settings.rotationMinutes=max(1,min(1440,settings.rotationMinutes))
        settings.retries=max(1,min(5,settings.retries))
        settings.startupTimeout=max(5,min(120,settings.startupTimeout))
        settings.qualityMbps=max(1,min(50,settings.qualityMbps))
        disk.save(settings,"settings.json")
        db.token=settings.githubToken
        configureLAN()
        log("INFO","settings","SAVED")
    }
    private func configureLAN() {
        if settings.lanEnabled {
            do {try lan.start()} catch {lanStatus="Failed to listen";log("ERROR","lan","START_FAILED")}
        } else {lan.stop();lanStatus="Disabled"}
    }
    private func configureAudio() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback,mode:.moviePlayback)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {log("ERROR","audio","SESSION_CONFIGURATION_FAILED")}
    }
    private func observeLifecycle() {
        let names:[Notification.Name]=[UIApplication.didEnterBackgroundNotification,UIApplication.willEnterForegroundNotification,UIApplication.didReceiveMemoryWarningNotification,AVAudioSession.interruptionNotification,AVAudioSession.routeChangeNotification]
        for name in names {
            let o=NotificationCenter.default.addObserver(forName:name,object:nil,queue:.main){ [weak self] _ in
                Task { @MainActor in
                    guard let self=self else {return}
                    self.log("INFO","lifecycle",name.rawValue)
                    if name==UIApplication.willEnterForegroundNotification {self.requestSync()}
                }
            }
            deviceObservers.append(o)
        }
        let center=MPRemoteCommandCenter.shared()
        center.playCommand.addTarget{[weak self] _ in Task{@MainActor in self?.resume()};return .success}
        center.pauseCommand.addTarget{[weak self] _ in Task{@MainActor in self?.pause()};return .success}
        center.nextTrackCommand.addTarget{[weak self] _ in Task{@MainActor in self?.next()};return .success}
    }
    func sanitized(_ value:String,limit:Int=8192)->String {
        var s=value
        for secret in [settings.password,settings.githubToken,jelly.token] where !secret.isEmpty {s=s.replacingOccurrences(of:secret,with:"[redacted]")}
        // URLSession errors can include a URL with an encoded access token.
        s=s.replacingOccurrences(of:"(?i)(api_key|access_token|token|password)=([^&\\s\"']+)",with:"$1=[redacted]",options:.regularExpression)
        return String(s.prefix(limit))
    }
    func log(_ severity:String,_ component:String,_ code:String,_ fields:[String:String]=[:]){
        let e=Event(severity:severity,component:component,code:code,trace:trace,fields:fields.mapValues{sanitized($0)})
        events.append(e);if events.count>300 {events.removeFirst(events.count-300)}
        disk.save(e,"event-\(e.id).json")
        if severity=="ERROR" || severity=="CRITICAL" {recalculateInsights()}
    }
    var eligibleChannels:[Channel] {channels.filter{!$0.isSurf && (health[$0.id]?.eligible(at:Date()) ?? true)}}
    var favorites:[Channel]{channels.filter(\.favorite)}
    var hiatus:[Channel]{channels.filter{c in health[c.id].map{!$0.banned && !$0.eligible(at:Date())} ?? false}}
    var banned:[Channel]{channels.filter{health[$0.id]?.banned == true}}
    var validatedCount:Int{health.values.filter{$0.successes>0 || $0.lifetimeFailures>0}.count}
    func connect() async {
        guard !loginBusy else{return};loginBusy=true;defer{loginBusy=false}
        // Drain the old stream's operations before replacing server credentials.
        let resumeLoop=settings.resumeSurf || surfing
        let category=settings.surfCategory
        let resumeChannel=current?.id
        let oldWork=work
        if current != nil || session != nil { clearPlayback() }
        await oldWork?.value
        await cleanupTask?.value
        catalogueReady=false
        lastLogin=Date();saveSettings();status="Connecting…"
        do {
            try await jelly.connect(settings)
            status="Connected"
            try await refresh()
            log("INFO","jellyfin","CONNECTED",["channels":String(channels.count)])
            failureRun=0
            loginBusy=false
            if resumeLoop && current==nil {startSurf(category:category)}
            else if let id=resumeChannel,let c=channels.first(where:{$0.id==id}),health[id]?.eligible(at:Date()) ?? true {open(c)}
        } catch {status="Connection failed";log("ERROR","jellyfin","CONNECT_FAILED",["error":error.localizedDescription])}
        recalculateInsights();requestSync()
    }
    func refresh() async throws {
        guard !catalogueBusy else{return};catalogueBusy=true;defer{catalogueBusy=false}
        let items=try await jelly.channels()
        channels=items;catalogueReady=true;disk.save(items,"channels.json");lastRefresh=Date()
        log("INFO","catalogue","REFRESHED",["count":String(items.count),"favorites":String(items.filter(\.favorite).count)])
        do {programs=try await jelly.programs();log("INFO","epg","REFRESHED",["count":String(programs.count)])}
        catch {log("WARN","epg","UNAVAILABLE")}
        recalculateInsights()
    }
    func refreshFromUI(){Task{do{try await refresh()}catch{log("ERROR","catalogue","REFRESH_FAILED",["error":error.localizedDescription])}}}
    func favorite(_ channel:Channel) async throws {
        guard jelly.connected,!loginBusy else{throw AppError.message("Jellyfin unavailable")}
        guard favoriteInFlight.insert(channel.id).inserted else{throw AppError.message("Favorite update already in progress")}
        defer{favoriteInFlight.remove(channel.id)}
        let value = !(channels.first(where:{$0.id==channel.id})?.favorite ?? channel.favorite)
        try await jelly.favorite(channel.id,value:value)
        if let i=channels.firstIndex(where:{$0.id==channel.id}){channels[i].favorite=value}
        if current?.id==channel.id {current?.favorite=value}
        if let i=previous.firstIndex(where:{$0.id==channel.id}){previous[i].favorite=value}
        disk.save(channels,"channels.json")
        log("INFO","favorites","UPDATED",["channelID":channel.id,"value":String(value)])
        requestSync()
    }
    func favoriteFromUI(_ channel:Channel){Task{do{try await favorite(channel)}catch{log("ERROR","favorites","FAILED",["error":error.localizedDescription])}}}
    func loadRecordings(){Task{do{recordings=try await jelly.recordings();log("INFO","dvr","RECORDINGS_LOADED",["count":String(recordings.count)])}catch{log("WARN","dvr","UNAVAILABLE")}}}
    func schedule(_ program:Program){Task{do{try await jelly.record(program);log("INFO","dvr","TIMER_CREATED",["programID":program.id])}catch{log("ERROR","dvr","TIMER_FAILED",["error":error.localizedDescription])}}}
    func startSurf(category:String="All Channels"){
        guard !loginBusy,jelly.connected,catalogueReady else{log("WARN","surf","NOT_CONNECTED");return}
        surfing=true;surfPaused=false;settings.resumeSurf=true;settings.surfCategory=category;disk.save(settings,"settings.json")
        attemptedIDs.removeAll();log("INFO","surf","START",["category":category,"minutes":String(settings.rotationMinutes)])
        next()
    }
    func stopSurf(){surfing=false;surfPaused=false;nextRotation=nil;pausedRotationSeconds=nil;emptyPoolRetry=nil;remaining=0;settings.resumeSurf=false;disk.save(settings,"settings.json");log("INFO","surf","STOP")}
    func toggleSurfPause(){
        guard surfing else{return}
        surfPaused.toggle()
        if surfPaused {
            pausedRotationSeconds=nextRotation.map{SurfClock.remaining(deadline:$0,now:Date())}
            nextRotation=nil
        } else if validated {
            nextRotation=SurfClock.resumed(now:Date(),remaining:pausedRotationSeconds ?? settings.rotationMinutes*60)
            pausedRotationSeconds=nil
        } else { emptyPoolRetry=Date() }
        log("INFO","surf",surfPaused ? "PAUSED":"RESUMED")
    }
    private func pool()->[Channel]{
        let all=eligibleChannels
        switch settings.surfCategory {
        case "Favorites":return all.filter(\.favorite)
        case "Sports":return all.filter{$0.genres.contains(where:{$0.localizedCaseInsensitiveContains("sport")})}
        case "Movies & Series":return all.filter{$0.genres.contains(where:{$0.localizedCaseInsensitiveContains("movie") || $0.localizedCaseInsensitiveContains("series") || $0.localizedCaseInsensitiveContains("drama")})}
        default:return all
        }
    }
    func next(){
        guard !loginBusy,jelly.connected,catalogueReady else{return}
        let candidates=pool()
        guard !candidates.isEmpty else {
            clearPlayback();nextRotation=nil;remaining=0;status="No eligible channels for this filter"
            emptyPoolRetry=Date().addingTimeInterval(60)
            log("WARN","surf","EMPTY_POOL",["category":settings.surfCategory]);return
        }
        emptyPoolRetry=nil
        var remainingCandidates=candidates.filter{!attemptedIDs.contains($0.id)}
        if remainingCandidates.isEmpty {attemptedIDs.removeAll();remainingCandidates=candidates}
        let filtered=remainingCandidates.filter{$0.id != current?.id}
        if !filtered.isEmpty {remainingCandidates=filtered}
        guard let channel=settings.random ? remainingCandidates.randomElement() : remainingCandidates.first else{return}
        attemptedIDs.insert(channel.id);begin(channel,keepSurf:true)
    }
    func open(_ c:Channel){
        guard !loginBusy,jelly.connected,catalogueReady else{log("WARN","player","NOT_CONNECTED");return}
        if c.isSurf {
            // App-owned Surf rotation identifies the actual channel for validation and Favorites.
            let category=String(c.name.dropFirst("Surf:".count)).trimmingCharacters(in:.whitespacesAndNewlines)
            startSurf(category:category)
        } else {stopSurf();begin(c,keepSurf:false)}
    }
    func openServerSurf(_ c:Channel){
        guard c.isSurf,!loginBusy,jelly.connected,catalogueReady else{return}
        stopSurf();begin(c,keepSurf:false)
    }
    private func begin(_ channel:Channel,keepSurf:Bool){
        guard !loginBusy,jelly.connected,catalogueReady else{log("WARN","player","NOT_CONNECTED");return}
        guard health[channel.id]?.eligible(at:Date()) ?? true else{status="Channel is excluded by validation policy";return}
        work?.cancel();generation=UUID();let g=generation
        retryCurrentAfterResume=false;pausedRotationSeconds=nil;lastPlayerError=nil
        let old=session;let oldPosition=position
        session=nil;player.pause();player.replaceCurrentItem(with:nil);videoOutput=nil
        if let old=old{queueClose(old,position:oldPosition)}
        let cleanup=cleanupTask
        if let c=current,c.id != channel.id{previous.removeAll{$0.id==c.id};previous.insert(c,at:0);if previous.count>30{previous.removeLast()}}
        current=channel;ready=false;playing=false;validated=false;intentionallyPaused=false;startedSession=false
        nextRotation=nil;remaining=0;lastAdvance=nil;lastPosition=0
        trace=UUID().uuidString;startUptime=ProcessInfo.processInfo.systemUptime
        status="Opening \(channel.name)"
        tab=1
        log("INFO","player","OPEN",["channelID":channel.id,"name":channel.name])
        work=Task{[weak self] in
            guard let self=self else{return}
            await cleanup?.value
            guard self.generation==g,!Task.isCancelled else{return}
            await self.attempt(channel,generation:g)
        }
    }
    private var position:Double {let n=player.currentTime().seconds;return n.isFinite ? max(0,n):0}
    private func attempt(_ channel:Channel,generation g:UUID) async {
        var errorReason="Startup timeout"
        for attemptIndex in 1...max(1,settings.retries) {
            guard g==generation,!Task.isCancelled else{return}
            errorReason="Startup timeout"
            log("INFO","validator","ATTEMPT",["channelID":channel.id,"attempt":String(attemptIndex)])
            do {
                let s=try await jelly.stream(channel,mbps:settings.qualityMbps)
                guard g==generation,!Task.isCancelled else{await jelly.close(s,position:0);return}
                session=s;startedSession=false
                let item=AVPlayerItem(url:s.url)
                let output=AVPlayerItemVideoOutput(pixelBufferAttributes:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA])
                item.add(output);videoOutput=output
                player.replaceCurrentItem(with:item)
                if !intentionallyPaused {player.play()}
                ready=false;lastPosition=position
                var deadline=Date().addingTimeInterval(Double(settings.startupTimeout))
                var firstPosition:Double?
                var evidence=VideoEvidence()
                while Date()<deadline && g==generation && !Task.isCancelled {
                    try? await Task.sleep(nanoseconds:500_000_000)
                    guard g==generation,!Task.isCancelled else{return}
                    if intentionallyPaused {
                        firstPosition=nil
                        evidence=VideoEvidence()
                        deadline=Date().addingTimeInterval(Double(settings.startupTimeout))
                        continue
                    }
                    if item.status == .failed {errorReason="AVPlayer item failed";break}
                    let p=position
                    if output.hasNewPixelBuffer(forItemTime:player.currentTime()) {
                        var display=CMTime.invalid
                        if output.copyPixelBuffer(forItemTime:player.currentTime(),itemTimeForDisplay:&display) != nil {
                            evidence.observe(frameTime:display.seconds,monotonic:ProcessInfo.processInfo.systemUptime)
                        }
                    }
                    if item.status == .readyToPlay && player.timeControlStatus == .playing {
                        if firstPosition==nil {firstPosition=p}
                        if let first=firstPosition,evidence.qualifies(progress:p-first,monotonic:ProcessInfo.processInfo.systemUptime) {
                            guard g==generation,!Task.isCancelled else{return}
                            validated=true;playing=true;lastSuccess=Date();lastAdvance=Date();lastPosition=p;failureRun=0
                            status="Playing \(channel.name)"
                            log("INFO","validator","PLAYBACK_CONFIRMED",["channelID":channel.id,"decodedSamples":String(evidence.frameCount),"displayReady":String(ready),"progressSeconds":String(format:"%.2f",p-first),"startupMs":String(Int((ProcessInfo.processInfo.systemUptime-startUptime)*1000))])
                            if settings.validation && !channel.isSurf {appendHealth(channel,"success","Playback progressed with video evidence")}
                            if surfing && !surfPaused {nextRotation=Date().addingTimeInterval(Double(settings.rotationMinutes*60))}
                            MPNowPlayingInfoCenter.default().nowPlayingInfo=[MPMediaItemPropertyTitle:channel.name,MPMediaItemPropertyArtist:channel.program?.title ?? "Live TV",MPNowPlayingInfoPropertyIsLiveStream:true,MPNowPlayingInfoPropertyPlaybackRate:1.0]
                            let report=lastProgressReport
                            lastProgressReport=Task{await report?.value;await jelly.report(s,action:"",position:p,paused:false)}
                            startedSession=true
                            requestSync();return
                        }
                    } else if !intentionallyPaused {firstPosition=nil}
                }
                if g != generation || Task.isCancelled {return}
                if let event=item.errorLog()?.events.last,(400...599).contains(event.errorStatusCode) {
                    errorReason="Stream HTTP \(event.errorStatusCode)"
                } else if let error=item.error {
                    let ns=error as NSError
                    errorReason=ns.domain==NSURLErrorDomain ? "Network player error \(ns.code)" : "Player error \(ns.code)"
                }
                session=nil;player.pause();player.replaceCurrentItem(with:nil);videoOutput=nil
                queueClose(s,position:0)
                await cleanupTask?.value
            } catch {errorReason=error.localizedDescription}
            guard g==generation,!Task.isCancelled else{return}
            log("WARN","validator","ATTEMPT_FAILED",["channelID":channel.id,"reason":errorReason,"attempt":String(attemptIndex)])
            try? await Task.sleep(nanoseconds:1_000_000_000)
        }
        guard g==generation,!Task.isCancelled else{return}
        if intentionallyPaused {retryCurrentAfterResume=true;return}
        let serverOK=await jelly.healthy()
        guard g==generation,!Task.isCancelled else{return}
        failureRun+=1
        // An infrastructure outage or unknown client-wide failure must not quarantine an entire catalogue.
        let recentSuccess=lastSuccess.map{Date().timeIntervalSince($0)<600} ?? false
        let foreground=UIApplication.shared.applicationState == .active
        let confirmed=PlaybackFailurePolicy.confirms(serverHealthy:serverOK,recentSuccess:recentSuccess,consecutiveFailures:failureRun,reason:errorReason,paused:intentionallyPaused || !foreground)
        lastPlayerError=errorReason
        if settings.validation && !channel.isSurf && confirmed {appendHealth(channel,"failure",errorReason)}
        log(confirmed ? "ERROR":"WARN","validator",confirmed ? "CHANNEL_FAILED":"FAILURE_UNCONFIRMED",["channelID":channel.id,"reason":errorReason,"serverHealthy":String(serverOK),"recentSuccess":String(recentSuccess)])
        status=confirmed ? "Channel failed; validation recorded":"Playback failure needs diagnosis"
        playing=false;validated=false
        if surfing && !surfPaused && serverOK && failureRun<3 {nextRotation=Date().addingTimeInterval(2)}
        else if surfing {emptyPoolRetry=Date().addingTimeInterval(30)}
        requestSync();recalculateInsights()
    }
    func presentationReady(_ value:Bool){
        if value && !ready{log("INFO","renderer","READY_FOR_DISPLAY",["elapsedMs":String(Int(max(0,ProcessInfo.processInfo.systemUptime-startUptime)*1000))])}
        ready=value
    }
    func pause(){
        guard !intentionallyPaused else{return}
        intentionallyPaused=true
        if let deadline=nextRotation {pausedRotationSeconds=SurfClock.remaining(deadline:deadline,now:Date());nextRotation=nil}
        player.pause();playing=false;log("INFO","player","PAUSE")
    }
    func resume(){
        intentionallyPaused=false
        lastAdvance=Date();lastPosition=position
        if retryCurrentAfterResume,let c=current {begin(c,keepSurf:surfing)}
        else if session != nil {
            player.play()
            if surfing && !surfPaused && validated {
                nextRotation=SurfClock.resumed(now:Date(),remaining:pausedRotationSeconds ?? settings.rotationMinutes*60)
            }
        }
        pausedRotationSeconds=nil
        log("INFO","player","RESUME")
    }
    private func queueClose(_ old:StreamSession,position p:Double){
        let prior=cleanupTask
        let report=lastProgressReport
        lastProgressReport=nil
        cleanupTask=Task{
            await prior?.value
            await report?.value
            await jelly.close(old,position:p)
        }
    }
    private func clearPlayback(){
        work?.cancel();generation=UUID();let old=session;let p=position
        session=nil;player.pause();player.replaceCurrentItem(with:nil);videoOutput=nil;current=nil;validated=false;playing=false;ready=false
        nextRotation=nil;remaining=0;retryCurrentAfterResume=false;lastAdvance=nil
        MPNowPlayingInfoCenter.default().nowPlayingInfo=nil
        if let old=old{queueClose(old,position:p)}
    }
    func stop(){
        stopSurf();clearPlayback()
        status="Stopped";log("INFO","player","STOP")
    }
    func appendHealth(_ channel:Channel,_ action:String,_ reason:String){
        ledger.events.append(HealthEvent(channelID:channel.id,channelName:channel.name,action:action,reason:sanitized(reason)))
        health=ledger.states();healthDirty=true;disk.save(ledger,"health.json")
        log("INFO","health","POLICY_CHANGED",["channelID":channel.id,"action":action,"failures":String(health[channel.id]?.failures ?? 0)])
        recalculateInsights();requestSync()
    }
    func approve(_ channel:Channel){
        guard health[channel.id]?.banned==true else{return}
        appendHealth(channel,"approve","Explicit administrator approval")
    }
    private func tick() async {
        let now=Date()
        let elapsed=now.timeIntervalSince(lastTick);lastTick=now
        if elapsed>5 {log("WARN","lifecycle","TICK_GAP",["seconds":String(format:"%.1f",elapsed)])}
        if let deadline=nextRotation,surfing,!surfPaused,!intentionallyPaused {
            remaining=SurfClock.remaining(deadline:deadline,now:now)
            if remaining==0{nextRotation=nil;next()}
        }
        if let retry=emptyPoolRetry,surfing,!surfPaused,!intentionallyPaused,retry<=now {emptyPoolRetry=nil;next()}
        if let item=player.currentItem,validated,!intentionallyPaused {
            let p=position
            if p>lastPosition+0.15 {lastAdvance=now;lastPosition=p}
            playing=player.timeControlStatus == .playing
            if item.status == .failed || (lastAdvance.map{now.timeIntervalSince($0)>30} ?? false) {
                log("WARN","recovery","PLAYBACK_STALLED",["position":String(p)])
                if let c=current {begin(c,keepSurf:surfing)}
            } else if now.timeIntervalSince(lastReport)>15 {
                lastReport=now
                if let s=session,startedSession {
                    let previousReport=lastProgressReport
                    lastProgressReport=Task{await previousReport?.value;await jelly.report(s,action:"Progress",position:p,paused:!playing)}
                }
                var values:[String:String]=["position":String(format:"%.2f",p),"rate":String(player.rate),"displayReady":String(ready),"thermal":String(ProcessInfo.processInfo.thermalState.rawValue),"battery":String(UIDevice.current.batteryLevel)]
                let ranges=item.loadedTimeRanges.compactMap{$0.timeRangeValue.duration.seconds.isFinite ? $0.timeRangeValue.duration.seconds:nil}
                values["bufferSeconds"]=String(format:"%.2f",ranges.reduce(0,+))
                if let a=item.accessLog()?.events.last {
                    values["observedBitrate"]=String(format:"%.0f",a.observedBitrate)
                    values["indicatedBitrate"]=String(format:"%.0f",a.indicatedBitrate)
                    values["droppedFrames"]=String(a.numberOfDroppedVideoFrames)
                    values["stalls"]=String(a.numberOfStalls)
                    values["bytes"]=String(a.numberOfBytesTransferred)
                }
                metrics=values;log("INFO","telemetry","PLAYBACK_SAMPLE",values)
            }
        }
        if jelly.connected && now.timeIntervalSince(lastRefresh)>180 {
            lastRefresh=now;Task{do{try await refresh()}catch{log("WARN","catalogue","REFRESH_FAILED")}}
        }
        if (!jelly.connected || status=="Connection failed") && settings.autoLogin && settings.unattended && !settings.server.isEmpty && now.timeIntervalSince(lastLogin)>60 {Task{await connect()}}
        if now.timeIntervalSince(lastSync)>30 {requestSync()}
        if now.timeIntervalSince(lastHeartbeat)>30 {
            lastHeartbeat=now;log("INFO","app","HEARTBEAT",["status":status,"channel":current?.id ?? "","surf":String(surfing),"eligible":String(eligibleChannels.count),"pendingEvents":String(disk.files(prefix:"event-").count)])
            recalculateInsights()
        }
    }
    func requestSync(){
        guard syncTask==nil else{return}
        db.token=settings.githubToken
        guard db.configured else{syncStatus="Configure TorrioDB token";return}
        syncTask=Task{[weak self] in
            guard let self=self else{return}
            await self.synchronize()
            self.syncTask=nil
        }
    }
    private func synchronize() async {
        lastSync=Date();syncStatus="Synchronizing"
        do {
            let merged=try await db.merge(ledger)
            ledger.merge(merged);health=ledger.states();disk.save(ledger,"health.json");healthDirty=false
            let prefix="control/devices/\(deviceID)"
            // Publish a small heartbeat before consuming pending commands.
            let snapshot=try JSONSerialization.data(withJSONObject:stateSnapshot(),options:[.sortedKeys])
            let old=try await db.read(prefix+"/heartbeat.json")
            try await db.put(prefix+"/heartbeat.json",data:snapshot,sha:old?.sha)
            let commandFiles=try await db.directory(prefix+"/commands").filter{validID(String($0.dropLast(5)))}
            let receiptFiles=Set(try await db.directory(prefix+"/receipts"))
            for file in CommandQueue.pending(commandFiles,receipts:receiptFiles,limit:20) {
                do {
                    guard let remote=try await db.read(prefix+"/commands/"+file) else{continue}
                    let expected=String(file.dropLast(5))
                    let result:Receipt
                    do {
                        let command=try CommandQueue.decode(remote.data,expectedID:expected)
                        result=await execute(command)
                    } catch {
                        result=Receipt(id:expected,action:"invalid",status:"failed",result:"Malformed command, schema or filename mismatch")
                    }
                    if result.status != "running" {
                        try await db.immutable(prefix+"/receipts/"+file,data:JSONCodec.encoder().encode(result))
                    }
                }catch{
                    // A malformed or temporarily unreadable file cannot block all later commands.
                    log("WARN","commands","REMOTE_COMMAND_SYNC_FAILED",["file":file,"error":error.localizedDescription])
                }
            }
            // Persist immutable batches before deleting their source records.
            for url in disk.files(prefix:"batch-").prefix(5) {
                let b=try Data(contentsOf:url)
                try await db.immutable("telemetry/\(deviceID)/"+url.lastPathComponent,data:b)
                disk.remove(url)
            }
            let urls=Array(disk.files(prefix:"event-").prefix(200))
            if !urls.isEmpty {
                let records=try urls.map{try JSONCodec.decoder().decode(Event.self,from:Data(contentsOf:$0))}
                let name="batch-\(UUID().uuidString).json"
                guard disk.save(records,name) else{throw AppError.message("Local batch write failed")}
                for url in urls{disk.remove(url)}
                let data=try Data(contentsOf:disk.root.appendingPathComponent(name))
                try await db.immutable("telemetry/\(deviceID)/"+name,data:data)
                disk.remove(disk.root.appendingPathComponent(name))
            }
            let insightBytes=try JSONCodec.encoder().encode(insights)
            let insightPath="insights/\(deviceID).json"
            let existing=try await db.read(insightPath)
            if existing?.data != insightBytes {try await db.put(insightPath,data:insightBytes,sha:existing?.sha)}
            syncStatus="Synced \(Date().formatted(date:.omitted,time:.shortened))"
        } catch {
            syncStatus="Sync pending: \(sanitized(error.localizedDescription))"
            log("WARN","torriodb","SYNC_FAILED",["error":error.localizedDescription])
        }
        recalculateInsights()
    }
    func recalculateInsights(){
        var list:[Insight]=[]
        if status=="Connection failed"{list.append(Insight(id:"connection",priority:"Emergency",title:"Jellyfin connection failed",explanation:"The application cannot authenticate or reach the server. Channel penalties are suspended for infrastructure failures.",action:"Check server availability and connection settings."))}
        if disk.error != nil {list.append(Insight(id:"disk",priority:"Emergency",title:"Local persistence needs attention",explanation:disk.error ?? "",action:"Check available device storage."))}
        if failureRun>=3 {list.append(Insight(id:"failure-run",priority:"High",title:"Multiple consecutive playback failures",explanation:"The validator cannot establish channel-specific failure. Automatic exclusions are restrained.",action:"Inspect server and player traces."))}
        if syncStatus.hasPrefix("Sync pending"){list.append(Insight(id:"sync",priority:"High",title:"TorrioDB synchronization delayed",explanation:syncStatus,action:"Check connectivity and repository access; local records remain queued."))}
        if surfing && pool().isEmpty {list.append(Insight(id:"empty-pool",priority:"Attention",title:"No eligible channels in Surf category",explanation:"The current category has no eligible channels or the server did not supply category metadata.",action:"Choose All Channels or another populated category. The scheduler will retry automatically."))}
        if settings.githubToken.isEmpty{list.append(Insight(id:"token",priority:"Attention",title:"TorrioDB is not configured",explanation:"Validation and telemetry are stored locally until repository access is configured.",action:"Enter a token in Settings."))}
        for c in channels where health[c.id]?.banned==true {list.append(Insight(id:"ban-"+c.id,priority:"High",title:c.name+" permanently excluded",explanation:"The final validation retry failed after progressive hiatus.",action:"Explicitly approve reinstatement only if desired."))}
        for c in channels where health[c.id]?.failures==7 && health[c.id]?.banned==false {list.append(Insight(id:"final-"+c.id,priority:"Attention",title:c.name+" at final hiatus",explanation:"The next confirmed failure after eligibility becomes permanent.",action:"Review its failure evidence."))}
        insights=list
        disk.save(list,"insights.json")
    }
    func stateSnapshot()->[String:Any]{
        ["schemaVersion":1,"deviceID":deviceID,"timestamp":ISO8601DateFormatter().string(from:Date()),"status":status,"currentChannel":current?.name ?? "","currentChannelID":current?.id ?? "","surfing":surfing,"surfPaused":surfPaused,"rotationMinutes":settings.rotationMinutes,"remainingSeconds":remaining,"channels":channels.count,"eligible":eligibleChannels.count,"hiatus":hiatus.count,"banned":banned.count,"playing":playing,"catalogueReady":catalogueReady,"lastPlayerError":lastPlayerError ?? "","metrics":metrics,"insights":insights.map{["priority":$0.priority,"title":$0.title]},"actions":Self.actions]
    }
    static let actions=["get_status","list_channels","open_channel","open_server_surf","play","pause","resume","stop","next_channel","previous_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","favorite_current","favorite_previous","get_channel_health","list_banned","list_hiatus","approve_reinstatement","get_logs","get_telemetry","get_insights","get_capabilities","sync_now","reconnect","script","get_version"]
    private func validID(_ id:String)->Bool{id.count<=100 && id.range(of:"^[A-Za-z0-9_-]+$",options:.regularExpression) != nil}
    func execute(_ c:Command) async -> Receipt {
        guard validID(c.id),c.schemaVersion==1 else{return Receipt(id:c.id,action:c.action,status:"failed",result:"Invalid command ID or schema")}
        if pending.contains(c.id){return Receipt(id:c.id,action:c.action,status:"running",result:"Command already executing")}
        if let previous=disk.load(Receipt.self,"receipt-\(c.id).json"){return previous}
        // A durable started receipt prevents replay after an interrupted mutation.
        let started=Receipt(id:c.id,action:c.action,status:"interrupted",result:"Execution started; outcome not yet recorded")
        guard disk.save(started,"receipt-\(c.id).json") else{return Receipt(id:c.id,action:c.action,status:"failed",result:"Cannot persist command")}
        pending.insert(c.id);defer{pending.remove(c.id)}
        log("INFO","commands","RECEIVED",["id":c.id,"action":c.action])
        var result=Receipt(id:c.id,action:c.action,status:"succeeded",result:"OK")
        do {
            if let expiry=c.expiresAt,expiry<Date(){throw AppError.message("Command expired")}
            let output=try await dispatch(c.action,args:c.arguments ?? [:])
            guard output.utf8.count<=524288 else{throw AppError.message("Response too large; request a paginated result")}
            // Structured query results contain only explicitly selected application fields.
            // Redacting a short password inside JSON syntax can corrupt numbers and IDs.
            let structured:Set<String>=["get_status","get_capabilities","list_channels","get_channel_health","list_banned","list_hiatus","get_logs","get_telemetry","get_insights"]
            result.result=structured.contains(c.action) ? output : sanitized(output,limit:524288)
        } catch {result.status="failed";result.result=sanitized(error.localizedDescription)}
        if !disk.save(result,"receipt-\(c.id).json") {
            result.status="interrupted";result.result="Operation finished but its receipt could not be persisted"
        }
        log(result.status=="succeeded" ? "INFO":"ERROR","commands","COMPLETED",["id":c.id,"action":c.action,"status":result.status])
        commandOutput=result.result
        return result
    }
    private func json<T:Encodable>(_ value:T) throws -> String{String(decoding:try JSONCodec.encoder().encode(value),as:UTF8.self)}
    private func dispatch(_ action:String,args:[String:String]) async throws -> String{
        switch action {
        case "get_status":return String(decoding:try JSONSerialization.data(withJSONObject:stateSnapshot(),options:[.sortedKeys]),as:UTF8.self)
        case "get_capabilities":return try json(Self.actions)
        case "get_version":return Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
        case "list_channels":
            let offset=max(0,min(channels.count,args["offset"].flatMap(Int.init) ?? 0))
            let limit=max(1,min(500,args["limit"].flatMap(Int.init) ?? 200))
            return try json(Array(channels.dropFirst(offset).prefix(limit)))
        case "open_server_surf":
            guard let id=args["id"],let c=channels.first(where:{$0.id==id && $0.isSurf}),jelly.connected,catalogueReady else{throw AppError.message("Unknown server Surf channel or offline")}
            openServerSurf(c);return "Server Surf playback requested; underlying channel validation unavailable"
        case "open_channel":
            guard let id=args["id"],let c=channels.first(where:{$0.id==id}) else{throw AppError.message("Unknown channel")}
            guard health[id]?.eligible(at:Date()) ?? true else{throw AppError.message("Channel is excluded")}
            guard jelly.connected else{throw AppError.message("Jellyfin is not connected")}
            open(c);return "Playback requested, not yet validated"
        case "play","resume":resume()
        case "pause":pause()
        case "stop":stop()
        case "next_channel":next()
        case "previous_channel":guard let c=previous.first else{throw AppError.message("No previous channel")};open(c)
        case "start_surf":
            let category=args["category"] ?? "All Channels"
            guard ["All Channels","Favorites","Sports","Movies & Series"].contains(category),jelly.connected,catalogueReady,!loginBusy else{throw AppError.message("Invalid category or Jellyfin unavailable")}
            startSurf(category:category)
        case "stop_surf":stopSurf()
        case "pause_surf":if !surfPaused{toggleSurfPause()}
        case "resume_surf":if surfPaused{toggleSurfPause()}
        case "set_rotation_minutes":
            guard let n=args["minutes"].flatMap(Int.init),(1...1440).contains(n) else{throw AppError.message("Minutes must be 1–1440")}
            settings.rotationMinutes=n;saveSettings();if surfing && !surfPaused && validated{nextRotation=Date().addingTimeInterval(Double(n*60))}
        case "favorite_current":guard let c=current,!c.isSurf else{throw AppError.message("No underlying channel selected")};try await favorite(c)
        case "favorite_previous":
            guard let id=args["id"],let c=previous.first(where:{$0.id==id}) else{throw AppError.message("Specify an immutable previous channel ID")};try await favorite(c)
        case "get_channel_health":return try json(health)
        case "list_banned":return try json(banned)
        case "list_hiatus":return try json(hiatus)
        case "approve_reinstatement":
            guard args["approval"]=="explicit",let id=args["id"],let c=channels.first(where:{$0.id==id}),health[id]?.banned==true else{throw AppError.message("Explicit approval and banned channel ID required")};approve(c)
        case "get_logs":return try json(Array(events.suffix(50)))
        case "get_telemetry":return try json(metrics)
        case "get_insights":return try json(insights)
        case "sync_now":requestSync()
        case "reconnect":await connect();guard jelly.connected,status != "Connection failed" else{throw AppError.message("Reconnect failed")}
        case "script":
            guard let source=args["source"] else{throw AppError.message("Missing script source")}
            return try await runScript(source)
        default:throw AppError.message("Unsupported command \(action)")
        }
        return "OK"
    }
    func runScript(_ source:String) async throws -> String {
        let start=ProcessInfo.processInfo.systemUptime
        let (output,commands)=try await scripts.evaluateAsync(source,snapshot:stateSnapshot())
        var results=[sanitized(output)]
        for c in commands where c.action != "script" && c.action != "approve_reinstatement" {
            let r=await execute(c);results.append("\(c.action): \(r.status) \(r.result)")
        }
        scriptOutput=sanitized(results.joined(separator:"\n"),limit:524288)
        log("INFO","runtime","SCRIPT_COMPLETED",["commands":String(commands.count),"ms":String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
        return scriptOutput
    }
}
