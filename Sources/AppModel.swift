import Foundation
import SwiftUI
import AVFoundation
import UIKit
import MediaPlayer
import Network

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
    @Published private(set) var systemMetrics:[String:String]=[:]
    @Published private(set) var telemetryActive=false
    @Published private(set) var telemetryLastSample:Date?
    private var telemetryCadence=TelemetryCadence()
    private let launchID=UUID().uuidString
    private let launchUptime=ProcessInfo.processInfo.systemUptime
    private let pathMonitor=NWPathMonitor()
    private var networkState="unknown"
    private var networkInterfaces="unknown"
    @Published var tab=0
    @Published var scriptOutput=""
    @Published var commandOutput=""
    let player=AVPlayer()
    let disk=Disk()
    let db=TorrioDB()
    let lan=LANServer()
    let scripts=ScriptEngine()
    private lazy var engineering=EngineeringRuntime(disk:disk,engine:scripts)
    @Published private(set) var campaign:DeviceTestCheckpoint?
    @Published private(set) var activePatchID="native-baseline"
    @Published private(set) var patchFault:String?
    private var policyOverride:RuntimePolicy?
    private var policyReady=false
    private var patchBusy=false
    private var campaignTask:Task<Void,Never>?
    private var campaignEpoch=UUID()
    private var backgroundTransitions=0
    private var selectionTask:Task<Void,Never>?
    private var selectionID=UUID()
    private var engineeringOwnsPlayback:Bool {campaign?.state=="running"}
    private var effectivePolicy:RuntimePolicy {policyOverride ?? RuntimePolicy.baseline(settings)}
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
        campaign=disk.load(DeviceTestCheckpoint.self,"device-campaign.json")
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
        telemetryActive=true
        log("INFO","app","START",["version":Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown","os":UIDevice.current.systemVersion,"launchID":launchID])
        sampleSystemTelemetry(force:true)
        startNetworkTelemetry()
        configureAudio()
        observeLifecycle()
        configureLAN()
        publishHeartbeat(now:Date())
        requestSync()
        Task { [weak self] in
            guard let self=self else{return}
            await self.bootRuntime()
            if self.settings.autoLogin && !self.settings.server.isEmpty {await self.connect()}
            self.resumeCampaignIfNeeded()
        }
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
                    if name==UIApplication.didEnterBackgroundNotification{self.backgroundTransitions+=1}
                    if name==UIApplication.willEnterForegroundNotification || name==UIApplication.didEnterBackgroundNotification {
                        self.sampleSystemTelemetry(force:true)
                        self.publishHeartbeat(now:Date())
                        self.requestSync()
                    }
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
            if !engineeringOwnsPlayback && resumeLoop && current==nil {startSurf(category:category)}
            else if !engineeringOwnsPlayback,let id=resumeChannel,let c=channels.first(where:{$0.id==id}),health[id]?.eligible(at:Date()) ?? true {open(c)}
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
    func stopSurf(){selectionTask?.cancel();selectionID=UUID();surfing=false;surfPaused=false;nextRotation=nil;pausedRotationSeconds=nil;emptyPoolRetry=nil;remaining=0;settings.resumeSurf=false;disk.save(settings,"settings.json");log("INFO","surf","STOP")}
    func toggleSurfPause(){
        guard surfing else{return}
        surfPaused.toggle()
        if surfPaused {
            pausedRotationSeconds=nextRotation.map{SurfClock.remaining(deadline:$0,now:Date())}
            nextRotation=nil
        } else if validated {
            nextRotation=SurfClock.resumed(now:Date(),remaining:pausedRotationSeconds ?? effectivePolicy.rotationSeconds)
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
        guard !loginBusy,jelly.connected,catalogueReady,policyReady else{return}
        selectionTask?.cancel();selectionID=UUID();let ticket=selectionID
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
        guard let fallback=settings.random ? remainingCandidates.randomElement() : remainingCandidates.first else{return}
        if activePatchID=="native-baseline" {
            attemptedIDs.insert(fallback.id);begin(fallback,keepSurf:true);return
        }
        let choices=remainingCandidates
        let previousID=current?.id
        selectionTask=Task{[weak self] in
            guard let self=self else{return}
            var selected=fallback
            do {
                if let id=try await self.engineering.select(candidates:choices,currentID:previousID),
                   let chosen=choices.first(where:{$0.id==id}){selected=chosen}
            }catch{self.log("WARN","runtime","SELECT_FALLBACK",["error":error.localizedDescription])}
            guard !Task.isCancelled,self.selectionID==ticket,self.health[selected.id]?.eligible(at:Date()) ?? true else{return}
            self.attemptedIDs.insert(selected.id);self.begin(selected,keepSurf:true)
        }
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
        guard !loginBusy,jelly.connected,catalogueReady,policyReady else{log("WARN","player","NOT_CONNECTED");return}
        guard health[channel.id]?.eligible(at:Date()) ?? true else{status="Channel is excluded by validation policy";return}
        work?.cancel();generation=UUID();let g=generation
        retryCurrentAfterResume=false;pausedRotationSeconds=nil;lastPlayerError=nil
        let old=session;let oldPosition=position
        session=nil;player.pause();player.replaceCurrentItem(with:nil);videoOutput=nil
        metrics=[:]
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
        let policy=effectivePolicy
        player.automaticallyWaitsToMinimizeStalling=policy.automaticallyWaits
        for attemptIndex in 1...max(1,policy.retries) {
            guard g==generation,!Task.isCancelled else{return}
            errorReason="Startup timeout"
            log("INFO","validator","ATTEMPT",["channelID":channel.id,"attempt":String(attemptIndex)])
            do {
                let s=try await jelly.stream(channel,mbps:policy.qualityMbps)
                guard g==generation,!Task.isCancelled else{await jelly.close(s,position:0);return}
                session=s;startedSession=false
                let item=AVPlayerItem(url:s.url)
                item.preferredForwardBufferDuration=Double(policy.forwardBufferSeconds)
                item.preferredPeakBitRate=Double(policy.qualityMbps)*1_000_000
                let output=AVPlayerItemVideoOutput(pixelBufferAttributes:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA])
                item.add(output);videoOutput=output
                player.replaceCurrentItem(with:item)
                if !intentionallyPaused {player.play()}
                ready=false;lastPosition=position
                var deadline=Date().addingTimeInterval(Double(policy.startupTimeout))
                var firstPosition:Double?
                var evidence=VideoEvidence()
                while Date()<deadline && g==generation && !Task.isCancelled {
                    try? await Task.sleep(nanoseconds:500_000_000)
                    guard g==generation,!Task.isCancelled else{return}
                    if intentionallyPaused {
                        firstPosition=nil
                        evidence=VideoEvidence()
                        deadline=Date().addingTimeInterval(Double(policy.startupTimeout))
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
                        if let first=firstPosition,evidence.qualifies(progress:p-first,monotonic:ProcessInfo.processInfo.systemUptime),
                           p-first>=Double(policy.validationSeconds),evidence.frameCount>=policy.minimumFrames {
                            guard g==generation,!Task.isCancelled else{return}
                            validated=true;playing=true;lastSuccess=Date();lastAdvance=Date();lastPosition=p;failureRun=0
                            status="Playing \(channel.name)"
                            log("INFO","validator","PLAYBACK_CONFIRMED",["channelID":channel.id,"decodedSamples":String(evidence.frameCount),"displayReady":String(ready),"progressSeconds":String(format:"%.2f",p-first),"startupMs":String(Int((ProcessInfo.processInfo.systemUptime-startUptime)*1000))])
                            if settings.validation && !engineeringOwnsPlayback && !channel.isSurf {appendHealth(channel,"success","Playback progressed with video evidence")}
                            if surfing && !surfPaused {nextRotation=Date().addingTimeInterval(Double(effectivePolicy.rotationSeconds))}
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
        if settings.validation && !engineeringOwnsPlayback && !channel.isSurf && confirmed {appendHealth(channel,"failure",errorReason)}
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
                nextRotation=SurfClock.resumed(now:Date(),remaining:pausedRotationSeconds ?? effectivePolicy.rotationSeconds)
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
        metrics=[:]
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
    private func startNetworkTelemetry(){
        pathMonitor.pathUpdateHandler={ [weak self] path in
            let status=String(describing:path.status)
            let interfaces:[(NWInterface.InterfaceType,String)]=[(.wifi,"wifi"),(.wiredEthernet,"ethernet"),(.cellular,"cellular"),(.loopback,"loopback"),(.other,"other")]
            let active=interfaces.filter{path.usesInterfaceType($0.0)}.map{$0.1}.joined(separator:",")
            let expensive=String(path.isExpensive)
            let constrained=String(path.isConstrained)
            Task{@MainActor in
                guard let self=self else{return}
                self.networkState=status
                self.networkInterfaces=active.isEmpty ? "none":active
                self.log("INFO","network","PATH_CHANGED",["status":status,"interfaces":self.networkInterfaces,"expensive":expensive,"constrained":constrained,"launchID":self.launchID])
                self.sampleSystemTelemetry(force:true)
            }
        }
        pathMonitor.start(queue:DispatchQueue(label:"jealousfin.network.telemetry"))
    }
    private func sampleSystemTelemetry(force:Bool=false){
        let process=ProcessInfo.processInfo
        let now=process.systemUptime
        guard telemetryActive,telemetryCadence.shouldSample(at:now,force:force) else{return}
        let device=UIDevice.current
        let appState:String
        switch UIApplication.shared.applicationState {
        case .active:appState="active"
        case .inactive:appState="inactive"
        case .background:appState="background"
        @unknown default:appState="unknown"
        }
        var fields:[String:String]=[
            "launchID":launchID,
            "appUptimeSeconds":String(format:"%.2f",max(0,now-launchUptime)),
            "systemUptimeSeconds":String(format:"%.2f",now),
            "appVersion":Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown",
            "buildNumber":Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "unknown",
            "osVersion":device.systemVersion,
            "deviceModel":device.model,
            "appState":appState,
            "thermalState":String(describing:process.thermalState),
            "lowPowerMode":String(process.isLowPowerModeEnabled),
            "physicalMemoryBytes":String(process.physicalMemory),
            "activeProcessorCount":String(process.activeProcessorCount),
            "batteryState":String(describing:device.batteryState),
            "networkState":networkState,
            "networkInterfaces":networkInterfaces,
            "jellyfinConnected":String(jelly.connected),
            "catalogueReady":String(catalogueReady),
            "playbackState":String(describing:player.timeControlStatus),
            "surfing":String(surfing),
            "syncStatus":sanitized(syncStatus),
            "lanStatus":lanStatus,
            "queuedEvents":String(disk.files(prefix:"event-").count),
            "queuedBatches":String(disk.files(prefix:"batch-").count)
        ]
        if device.batteryLevel>=0 {fields["batteryPercent"]=String(format:"%.0f",device.batteryLevel*100)}
        if let attributes=try? FileManager.default.attributesOfFileSystem(forPath:disk.root.path),
           let free=attributes[.systemFreeSize] as? NSNumber {
            fields["storageFreeBytes"]=free.stringValue
        }
        systemMetrics=fields
        telemetryLastSample=Date()
        log("INFO","telemetry","SYSTEM_SAMPLE",fields)
    }
    private func publishHeartbeat(now:Date){
        lastHeartbeat=now
        log("INFO","app","HEARTBEAT",["launchID":launchID,"status":status,"channel":current?.id ?? "","surf":String(surfing),"eligible":String(eligibleChannels.count),"pendingEvents":String(disk.files(prefix:"event-").count)])
        recalculateInsights()
    }
    private func tick() async {
        let now=Date()
        sampleSystemTelemetry()
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
            if item.status == .failed || (lastAdvance.map{now.timeIntervalSince($0)>Double(effectivePolicy.stallSeconds)} ?? false) {
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
        if policyReady && (!jelly.connected || status=="Connection failed") && settings.autoLogin && settings.unattended && !settings.server.isEmpty && now.timeIntervalSince(lastLogin)>60 {Task{await connect()}}
        resumeCampaignIfNeeded()
        if now.timeIntervalSince(lastHeartbeat)>30 {publishHeartbeat(now:now)}
        if now.timeIntervalSince(lastSync)>30 {requestSync()}
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
    private func uploadTelemetry() async throws {
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
    }
    private func synchronize() async {
        lastSync=Date();syncStatus="Synchronizing"
        // Telemetry must not depend on health-ledger merge or command success.
        var telemetryError:String?
        do {try await uploadEngineeringResults()}
        catch {log("WARN","engineering","RESULT_UPLOAD_FAILED",["error":error.localizedDescription])}
        do {try await uploadTelemetry()}
        catch {telemetryError=sanitized(error.localizedDescription);log("WARN","torriodb","TELEMETRY_UPLOAD_FAILED",["error":error.localizedDescription])}
        do {
            do {
                let merged=try await db.merge(ledger)
                ledger.merge(merged);health=ledger.states();disk.save(ledger,"health.json");healthDirty=false
            }catch{log("WARN","health","MERGE_FAILED",["error":error.localizedDescription])}
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
            let insightBytes=try JSONCodec.encoder().encode(insights)
            let insightPath="insights/\(deviceID).json"
            let existing=try await db.read(insightPath)
            if existing?.data != insightBytes {try await db.put(insightPath,data:insightBytes,sha:existing?.sha)}
            if let error=telemetryError {syncStatus="Sync pending: telemetry "+error}
            else {syncStatus="Synced \(Date().formatted(date:.omitted,time:.shortened))"}
        } catch {
            syncStatus="Sync pending: \(sanitized(error.localizedDescription))"
            log("WARN","torriodb","SYNC_FAILED",["error":error.localizedDescription])
        }
        recalculateInsights()
    }
    func recalculateInsights(){
        var list:[Insight]=[]
        if let fault=patchFault {list.append(Insight(id:"patch",priority:"High",title:"Runtime module needs attention",explanation:fault,action:"Inspect module status and roll back or install a corrected module."))}
        if let result=campaign?.lastResult,result.status=="failed" {list.append(Insight(id:"device-test",priority:"High",title:"On-device test failed: "+result.kind,explanation:result.evidence["reason"] ?? "See the correlated engineering result.",action:"Inspect campaign evidence and revise the runtime policy."))}
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
        ["schemaVersion":1,"deviceID":deviceID,"timestamp":ISO8601DateFormatter().string(from:Date()),"status":status,"currentChannel":current?.name ?? "","currentChannelID":current?.id ?? "","surfing":surfing,"surfPaused":surfPaused,"rotationMinutes":settings.rotationMinutes,"remainingSeconds":remaining,"channels":channels.count,"eligible":eligibleChannels.count,"hiatus":hiatus.count,"banned":banned.count,"playing":playing,"catalogueReady":catalogueReady,"lastPlayerError":lastPlayerError ?? "","metrics":metrics,"systemMetrics":systemMetrics,"telemetryActive":telemetryActive,"telemetryLastSample":telemetryLastSample.map{ISO8601DateFormatter().string(from:$0)} ?? "","launchID":launchID,"runtimePatch":activePatchID,"runtimeABI":1,"runtimeReady":policyReady,"campaignID":campaign?.plan.id ?? "","campaignState":campaign?.state ?? "idle","campaignCycle":campaign?.cycle ?? 0,"campaignStep":campaign?.stepIndex ?? 0,"insights":insights.map{["priority":$0.priority,"title":$0.title]},"actions":Self.actions]
    }
    static let actions=["get_status","list_channels","open_channel","open_server_surf","play","pause","resume","stop","next_channel","previous_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","favorite_current","favorite_previous","get_channel_health","list_banned","list_hiatus","approve_reinstatement","get_logs","get_telemetry","get_insights","get_capabilities","sync_now","reconnect","script","get_version","install_runtime_module","reload_runtime_module","rollback_runtime_module","reset_runtime_module","get_runtime_module","start_device_tests","pause_device_tests","resume_device_tests","stop_device_tests","get_device_tests"]
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
            let structured:Set<String>=["get_status","get_capabilities","list_channels","get_channel_health","list_banned","list_hiatus","get_logs","get_telemetry","get_insights","get_runtime_module","get_device_tests"]
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
        case "get_telemetry":
            return String(decoding:try JSONSerialization.data(withJSONObject:["active":telemetryActive,"launchID":launchID,"lastSample":telemetryLastSample.map{ISO8601DateFormatter().string(from:$0)} ?? "","system":systemMetrics,"playback":metrics],options:[.sortedKeys]),as:UTF8.self)
        case "get_insights":return try json(insights)
        case "sync_now":requestSync()
        case "reconnect":await connect();guard jelly.connected,status != "Connection failed" else{throw AppError.message("Reconnect failed")}
        case "install_runtime_module":
            guard let id=args["id"],let source=args["source"] else{throw AppError.message("Module ID and JavaScript source required")}
            try await installRuntime(id:id,source:source);return "Runtime module activated: "+activePatchID
        case "reload_runtime_module":
            guard policyReady,!patchBusy else{throw AppError.message("Runtime busy")}
            await bootRuntime();return "Runtime reloaded: "+activePatchID
        case "rollback_runtime_module":
            try await changeRuntime(reset:false);return "Runtime rolled back: "+activePatchID
        case "reset_runtime_module":
            try await changeRuntime(reset:true);return "Native baseline restored"
        case "get_runtime_module":
            let data=try JSONCodec.encoder().encode(effectivePolicy)
            let p=try JSONSerialization.jsonObject(with:data)
            return String(decoding:try JSONSerialization.data(withJSONObject:["abi":1,"id":activePatchID,"ready":policyReady,"fault":patchFault ?? "","policy":p],options:[.sortedKeys]),as:UTF8.self)
        case "start_device_tests":
            guard let plan=args["plan"] else{throw AppError.message("Provide a JSON test plan")}
            try startCampaign(try JSONCodec.decoder().decode(DeviceTestPlan.self,from:Data(plan.utf8)));return "Campaign persisted and queued"
        case "pause_device_tests":try setCampaignState("paused")
        case "resume_device_tests":try setCampaignState("queued");resumeCampaignIfNeeded()
        case "stop_device_tests":try setCampaignState("stopped")
        case "get_device_tests":return try json(campaign)
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
        for c in commands where c.action != "script" && c.action != "approve_reinstatement" && !["install_runtime_module","reload_runtime_module","rollback_runtime_module","reset_runtime_module"].contains(c.action) {
            let r=await execute(c);results.append("\(c.action): \(r.status) \(r.result)")
        }
        scriptOutput=sanitized(results.joined(separator:"\n"),limit:524288)
        log("INFO","runtime","SCRIPT_COMPLETED",["commands":String(commands.count),"ms":String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
        return scriptOutput
    }
}


// The test runner uses the same player and Jellyfin interfaces as ordinary viewing.
// All test mutations are checkpointed. Test outcomes do not modify the production health ledger.
extension AppModel {
    private func applyRuntime(_ policy:RuntimePolicy?) {
        policyOverride=policy
        activePatchID=engineering.activeID
        patchFault=engineering.fault
        player.automaticallyWaitsToMinimizeStalling=effectivePolicy.automaticallyWaits
        if let item=player.currentItem {
            item.preferredForwardBufferDuration=Double(effectivePolicy.forwardBufferSeconds)
            item.preferredPeakBitRate=Double(effectivePolicy.qualityMbps)*1_000_000
        }
        if surfing && !surfPaused && !intentionallyPaused && validated {
            nextRotation=Date().addingTimeInterval(Double(effectivePolicy.rotationSeconds))
        }
        sampleSystemTelemetry(force:true)
        recalculateInsights()
    }
    private func suspendCampaignForPatch() throws {
        if campaign?.state=="running" {
            try setCampaignState("queued")
        }
    }
    private func bootRuntime() async {
        guard !patchBusy else{return}
        patchBusy=true;policyReady=false
        defer{patchBusy=false;policyReady=true;resumeCampaignIfNeeded();requestSync()}
        do {
            try suspendCampaignForPatch()
            let p=try await engineering.boot(settings:settings)
            applyRuntime(engineering.activeID=="native-baseline" ? nil:p)
            log(patchFault==nil ? "INFO":"WARN","runtime","MODULE_LOADED",["module":activePatchID,"fault":patchFault ?? ""])
        }catch{
            policyOverride=nil;activePatchID="native-baseline"
            patchFault=sanitized(error.localizedDescription)
            log("ERROR","runtime","BOOT_FALLBACK",["error":patchFault ?? ""])
        }
    }
    private func installRuntime(id:String,source:String) async throws {
        guard policyReady,!patchBusy else{throw AppError.message("Runtime not ready")}
        patchBusy=true
        defer{patchBusy=false;resumeCampaignIfNeeded();requestSync()}
        try suspendCampaignForPatch()
        do {
            let p=try await engineering.install(id:id,source:source,settings:settings)
            applyRuntime(p)
            log("INFO","runtime","MODULE_ACTIVATED",["module":id])
        }catch{
            patchFault=sanitized(error.localizedDescription)
            log("ERROR","runtime","MODULE_REJECTED",["module":id,"error":patchFault ?? ""])
            throw error
        }
    }
    private func changeRuntime(reset:Bool) async throws {
        guard policyReady,!patchBusy else{throw AppError.message("Runtime not ready")}
        patchBusy=true
        defer{patchBusy=false;resumeCampaignIfNeeded();requestSync()}
        try suspendCampaignForPatch()
        let p:RuntimePolicy
        if reset {p=try engineering.reset(settings:settings)}
        else {p=try await engineering.rollback(settings:settings)}
        applyRuntime(engineering.activeID=="native-baseline" ? nil:p)
        log("INFO","runtime","MODULE_ROLLBACK",["module":activePatchID])
    }

    private func saveCampaign(_ value:DeviceTestCheckpoint) throws {
        guard disk.save(value,"device-campaign.json") else{throw AppError.message("Cannot persist test checkpoint")}
        campaign=value
    }
    func startCampaign(_ plan:DeviceTestPlan)throws{
        let plan=try plan.checked()
        guard policyReady,!patchBusy else{throw AppError.message("Runtime not ready")}
        guard campaignTask==nil,!(campaign.map{["queued","running","paused"].contains($0.state)} ?? false) else{throw AppError.message("Stop or finish the existing campaign first")}
        try saveCampaign(DeviceTestCheckpoint(plan:plan))
        stop()
        log("INFO","engineering","CAMPAIGN_CREATED",["plan":plan.id,"steps":String(plan.steps.count),"repeatForever":String(plan.repeatForever)])
        resumeCampaignIfNeeded()
        requestSync()
    }
    func setCampaignState(_ state:String)throws{
        guard var cp=campaign else{throw AppError.message("No device test campaign")}
        guard ["queued","paused","stopped"].contains(state) else{throw AppError.message("Invalid campaign state")}
        if state=="queued" {
            guard cp.state != "completed",cp.state != "stopped" else{throw AppError.message("Create a new plan for a completed or stopped campaign")}
        }
        cp.state=state;cp.updatedAt=Date()
        try saveCampaign(cp)
        campaignEpoch=UUID();campaignTask?.cancel()
        stop()
        log("INFO","engineering","CAMPAIGN_STATE",["plan":cp.plan.id,"state":state])
        requestSync()
    }
    private func resumeCampaignIfNeeded(){
        guard policyReady,!patchBusy,campaignTask==nil,let cp=campaign,cp.canResume else{return}
        // Re-enter the current checkpoint after a process restart. Never infer that
        // an interrupted step succeeded.
        campaignEpoch=UUID();let epoch=campaignEpoch
        campaignTask=Task{[weak self] in
            guard let self=self else{return}
            await self.runCampaign(epoch:epoch)
            self.campaignTask=nil
        }
    }
    private func campaignCurrent(_ epoch:UUID)->Bool {
        !Task.isCancelled && campaignEpoch==epoch && campaign?.canResume==true
    }
    private func runCampaign(epoch:UUID) async {
        while campaignCurrent(epoch),var cp=campaign {
            do{
                if cp.state != "running" {
                    cp.state="running";cp.updatedAt=Date();try saveCampaign(cp)
                }
                let file="qa-result-"+cp.resultID+".json"
                if let existing=disk.load(DeviceTestResult.self,file) {
                    cp.advance(existing);try saveCampaign(cp)
                    requestSync()
                }else{
                    let step=cp.plan.steps[cp.stepIndex]
                    // A test plan starts only when commanded. Offline steps report
                    // blocked instead of pretending that the hardware passed.
                    let started=Date()
                    let module=activePatchID
                    log("INFO","engineering","STEP_STARTED",["plan":cp.plan.id,"cycle":String(cp.cycle),"step":String(cp.stepIndex),"kind":step.kind,"module":module])
                    let observation=await runDeviceStep(step,epoch:epoch)
                    guard campaignCurrent(epoch),let actual=campaign,actual.runID==cp.runID,
                          actual.stepIndex==cp.stepIndex,actual.cycle==cp.cycle else{return}
                    let result=DeviceTestResult(id:cp.resultID,planID:cp.plan.id,cycle:cp.cycle,stepIndex:cp.stepIndex,kind:step.kind,
                        channelID:observation.channelID ?? step.channelID,startedAt:started,status:observation.status,
                        evidence:observation.evidence.mapValues{sanitized($0)},launchID:launchID,patchID:module)
                    guard disk.save(result,file) else{throw AppError.message("Cannot persist device test result")}
                    cp.advance(result);try saveCampaign(cp)
                    log(result.status=="failed" ? "ERROR":"INFO","engineering","STEP_RESULT",["plan":cp.plan.id,"kind":step.kind,"status":result.status,"resultID":result.id])
                    recalculateInsights();requestSync()
                }
                if cp.state=="completed" {
                    stop();log("INFO","engineering","CAMPAIGN_COMPLETED",["plan":cp.plan.id,"cycles":String(cp.cycle)])
                    requestSync();return
                }
                // Avoid a tight retry loop for an offline device or a zero-work plan.
                try await Task.sleep(nanoseconds:2_000_000_000)
            }catch{
                if !campaignCurrent(epoch){return}
                var failed=cp;failed.state="paused";failed.updatedAt=Date()
                try? saveCampaign(failed)
                stop()
                log("ERROR","engineering","CAMPAIGN_PERSISTENCE_FAILED",["error":error.localizedDescription])
                return
            }
        }
    }
    private struct Observation {
        var status:String
        var evidence:[String:String]
        var channelID:String? = nil
    }
    private func nap(_ seconds:Double) async {
        try? await Task.sleep(nanoseconds:UInt64(max(0,min(seconds,3600))*1_000_000_000))
    }
    private func awaitPlayback(epoch:UUID,id:String,seconds:Int) async -> Bool{
        let deadline=ProcessInfo.processInfo.systemUptime+Double(seconds)
        while campaignCurrent(epoch),ProcessInfo.processInfo.systemUptime<deadline {
            if current?.id==id && validated && player.timeControlStatus == .playing {return true}
            if current?.id==id && lastPlayerError != nil && player.currentItem==nil{return false}
            await nap(0.5)
        }
        return false
    }
    private func runDeviceStep(_ step:DeviceTestStep,epoch:UUID) async -> Observation {
        let seconds=step.seconds ?? 30
        let start=ProcessInfo.processInfo.systemUptime
        switch step.kind {
        case "system":
            sampleSystemTelemetry(force:true)
            var evidence=systemMetrics
            evidence["telemetryActive"]=String(telemetryActive)
            return Observation(status:telemetryActive && telemetryLastSample != nil ? "passed":"failed",evidence:evidence)
        case "catalogue":
            guard jelly.connected else{return Observation(status:"blocked",evidence:["reason":"Jellyfin is not connected"])}
            do{
                // Query actual server instead of treating cached channels as live evidence.
                let fresh=try await jelly.channels()
                return Observation(status:fresh.isEmpty ? "failed":"passed",evidence:["channelCount":String(fresh.count),"requestMs":String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
            }catch{return Observation(status:"failed",evidence:["reason":error.localizedDescription])}
        case "stop":
            stop()
            let close=cleanupTask
            await close?.value
            return Observation(status:player.currentItem==nil && player.rate==0 && current==nil ? "passed":"failed",evidence:["playerItemReleased":String(player.currentItem==nil),"rate":String(player.rate),"sessionCleared":String(session==nil)])
        default:break
        }
        guard jelly.connected,catalogueReady else{return Observation(status:"blocked",evidence:["reason":"Live Jellyfin connection required"])}
        if step.kind=="surf_rotation" {
            guard eligibleChannels.count>=2 else{return Observation(status:"blocked",evidence:["reason":"At least two eligible real channels required"])}
            startSurf(category:"All Channels")
            let deadline=ProcessInfo.processInfo.systemUptime+Double(seconds)
            var validatedIDs=Set<String>()
            while campaignCurrent(epoch),ProcessInfo.processInfo.systemUptime<deadline {
                if surfing,validated,player.timeControlStatus == .playing,let id=current?.id{validatedIDs.insert(id)}
                if validatedIDs.count>=2 {
                    return Observation(status:"passed",evidence:["validatedChannelIDs":validatedIDs.sorted().joined(separator:","),"elapsedSeconds":String(format:"%.2f",ProcessInfo.processInfo.systemUptime-start)])
                }
                await nap(0.5)
            }
            return Observation(status:"failed",evidence:["reason":"Two distinct validated rotations were not observed within the test window","validatedCount":String(validatedIDs.count)])
        }
        let channel:Channel?
        if let id=step.channelID{channel=eligibleChannels.first(where:{$0.id==id})}
        else {channel=eligibleChannels.first(where:{$0.id==current?.id}) ?? eligibleChannels.first}
        guard let channel=channel else{return Observation(status:"blocked",evidence:["reason":"Requested channel is not eligible"],channelID:step.channelID)}
        open(channel)
        let startupBudget=max(15,min(720,effectivePolicy.retries*(effectivePolicy.startupTimeout+45)))
        guard await awaitPlayback(epoch:epoch,id:channel.id,seconds:startupBudget) else {
            return Observation(status:"failed",evidence:["reason":lastPlayerError ?? "No confirmed playback before deadline","startupBudgetSeconds":String(startupBudget)],channelID:channel.id)
        }
        let startupMs=Int((ProcessInfo.processInfo.systemUptime-start)*1000)
        let streamGeneration=generation
        var evidence:[String:String]=["startupMs":String(startupMs),"channel":channel.name,"displayReady":String(ready)]
        if step.kind=="pause_resume" {
            pause();await nap(0.75)
            let p=position
            await nap(Double(max(2,min(seconds,10))))
            let delta=abs(position-p)
            evidence["pausedClockDelta"]=String(format:"%.3f",delta)
            guard campaignCurrent(epoch),generation==streamGeneration else{return Observation(status:"blocked",evidence:["reason":"Playback changed during pause test"],channelID:channel.id)}
            resume()
            if delta>0.5{return Observation(status:"failed",evidence:evidence.merging(["reason":"Playback clock advanced while paused"]){_,b in b},channelID:channel.id)}
        }
        let before=position
        let observationStart=ProcessInfo.processInfo.systemUptime
        let backgroundBaseline=backgroundTransitions
        let deadline=observationStart+Double(step.kind=="pause_resume" ? 8:max(4,seconds))
        var frames=VideoEvidence()
        var maxGap=0.0
        var lastFrame=observationStart
        var backgroundSamples=0
        var backgroundProgress=0.0
        var prior=before
        var priorWasBackground=false
        var maxTickGap=0.0
        var lastTick=observationStart
        while campaignCurrent(epoch),ProcessInfo.processInfo.systemUptime<deadline {
            guard generation==streamGeneration,current?.id==channel.id,let item=player.currentItem else {
                return Observation(status:"failed",evidence:evidence.merging(["reason":"Playback generation changed during observation"]){_,b in b},channelID:channel.id)
            }
            let now=ProcessInfo.processInfo.systemUptime
            maxTickGap=max(maxTickGap,now-lastTick);lastTick=now
            if item.status == .failed {
                return Observation(status:"failed",evidence:evidence.merging(["reason":"AVPlayer item failed during observation"]){_,b in b},channelID:channel.id)
            }
            let background=UIApplication.shared.applicationState == .background
            if background {
                backgroundSamples+=1
                if priorWasBackground{backgroundProgress+=max(0,position-prior)}
            }
            prior=position;priorWasBackground=background
            if let output=videoOutput,output.hasNewPixelBuffer(forItemTime:player.currentTime()){
                var display=CMTime.invalid
                if output.copyPixelBuffer(forItemTime:player.currentTime(),itemTimeForDisplay:&display) != nil {
                    let oldCount=frames.frameCount
                    frames.observe(frameTime:display.seconds,monotonic:now)
                    if frames.frameCount>oldCount{maxGap=max(maxGap,now-lastFrame);lastFrame=now}
                }
            }
            await nap(0.5)
        }
        let finish=ProcessInfo.processInfo.systemUptime
        let progress=position-before
        evidence["observedSeconds"]=String(format:"%.2f",finish-observationStart)
        evidence["clockProgress"]=String(format:"%.2f",progress)
        evidence["decodedSamples"]=String(frames.frameCount)
        evidence["maximumFrameSampleGap"]=String(format:"%.2f",max(maxGap,finish-lastFrame))
        evidence["maximumExecutionGap"]=String(format:"%.2f",maxTickGap)
        if let access=player.currentItem?.accessLog()?.events.last {
            evidence["droppedFrames"]=String(access.numberOfDroppedVideoFrames)
            evidence["stalls"]=String(access.numberOfStalls)
            evidence["observedBitrate"]=String(format:"%.0f",access.observedBitrate)
        }
        if step.kind=="background" {
            evidence["backgroundTransitions"]=String(backgroundTransitions-backgroundBaseline)
            evidence["backgroundSamples"]=String(backgroundSamples)
            evidence["backgroundClockProgress"]=String(format:"%.2f",backgroundProgress)
            guard backgroundSamples>=2 else{
                evidence["reason"]="No actual background execution observed; test needs a real Home/lock transition"
                return Observation(status:"blocked",evidence:evidence,channelID:channel.id)
            }
            let ok=backgroundProgress>=3 && maxTickGap<=5
            if !ok{evidence["reason"]="Background clock progression or execution cadence did not meet threshold"}
            return Observation(status:ok ? "passed":"failed",evidence:evidence,channelID:channel.id)
        }
        let duration=max(0,finish-observationStart)
        let enough=progress>=max(3,duration*0.70) && frames.qualifies(progress:progress,monotonic:finish)
        let continuous=step.kind != "endurance" || max(maxGap,finish-lastFrame)<=5
        let ok=enough && continuous
        if !ok{evidence["reason"]="Insufficient continuous video evidence or media clock progression"}
        return Observation(status:ok ? "passed":"failed",evidence:evidence,channelID:channel.id)
    }
    private func uploadEngineeringResults() async throws {
        // Keep completed step evidence until the corresponding checkpoint is durable.
        for url in disk.files(prefix:"qa-result-").prefix(12) {
            if let cp=campaign,cp.canResume,url.lastPathComponent=="qa-result-"+cp.resultID+".json"{continue}
            let data=try Data(contentsOf:url)
            let result=try JSONCodec.decoder().decode(DeviceTestResult.self,from:data)
            try await db.immutable("engineering/"+deviceID+"/results/"+result.id+".json",data:data)
            disk.remove(url)
        }
        if let cp=campaign {
            let path="engineering/"+deviceID+"/checkpoint.json"
            let data=try JSONCodec.encoder().encode(cp)
            let existing=try await db.read(path)
            if existing?.data != data {try await db.put(path,data:data,sha:existing?.sha)}
        }
    }
}
