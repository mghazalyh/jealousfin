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
    @Published private(set) var jellyfinAPIRevision="native-baseline"
    @Published private(set) var jellyfinAPIFault:String?
    private var jellyfinAPIStore=JellyfinAdapterStore()
    private var apiChanging=false
    private var apiReady=false
    @Published private(set) var dynamicUI=DynamicUIStore()
    @Published private(set) var decisions=DecisionJournal()
    private var decisionTask:Task<Void,Never>?
    @Published var dynamicPage:DynamicUIPage?
    @Published private(set) var dynamicUIResults:[String:String]=[:]
    @Published private(set) var dynamicUIBusy=Set<String>()
    private var dynamicUISeen=Set<String>()
    private lazy var engineering=EngineeringRuntime(disk:disk,engine:scripts)
    @Published private(set) var campaign:DeviceTestCheckpoint?
    @Published private(set) var activePatchID="native-baseline"
    @Published private(set) var patchFault:String?
    private var policyOverride:RuntimePolicy?
    private var policyReady=false
    private var patchBusy=false
    private var moduleTask:Task<Void,Never>?
    private var moduleCheckpoint:ModuleCheckpoint?
    private var moduleEventCursor:String?
    private var moduleFailures=0
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
        jellyfinAPIStore=disk.load(JellyfinAdapterStore.self,"jellyfin-api.json") ?? JellyfinAdapterStore()
        campaign=disk.load(DeviceTestCheckpoint.self,"device-campaign.json")
        decisions=disk.load(DecisionJournal.self,"decision-journal.json") ?? DecisionJournal()
        if var stored=disk.load(DynamicUIStore.self,"dynamic-ui.json"){
            if let layout=stored.active,(try? layout.checked())==nil {
                stored.active=nil;stored.values=[:]
            }
            if let previous=stored.previous,(try? previous.checked())==nil {stored.previous=nil}
            if let layout=stored.active {
                stored.values=layout.retainedValues(stored.values)
            }else{stored.values=[:]}
            dynamicUI=stored
        }
        moduleCheckpoint=disk.load(ModuleCheckpoint.self,"module-checkpoint.json")
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
        log("INFO","dynamic-ui","LAYOUT_RESTORED",["revision":dynamicUI.active?.revision ?? "native-baseline"])
        startNetworkTelemetry()
        configureAudio()
        observeLifecycle()
        configureLAN()
        publishHeartbeat(now:Date())
        requestSync()
        Task { [weak self] in
            guard let self=self else{return}
            await self.bootJellyfinAPI()
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
        guard apiReady,!apiChanging,!loginBusy else{return};loginBusy=true;defer{loginBusy=false}
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
        guard !apiChanging,!catalogueBusy else{return};catalogueBusy=true;defer{catalogueBusy=false}
        let items=try await jelly.channels()
        channels=items;catalogueReady=true;disk.save(items,"channels.json");lastRefresh=Date()
        log("INFO","catalogue","REFRESHED",["count":String(items.count),"favorites":String(items.filter(\.favorite).count)])
        do {programs=try await jelly.programs();log("INFO","epg","REFRESHED",["count":String(programs.count)])}
        catch {log("WARN","epg","UNAVAILABLE")}
        recalculateInsights()
    }
    func refreshFromUI(){Task{do{try await refresh()}catch{log("ERROR","catalogue","REFRESH_FAILED",["error":error.localizedDescription])}}}
    func favorite(_ channel:Channel) async throws {
        guard !apiChanging,jelly.connected,!loginBusy else{throw AppError.message("Jellyfin unavailable")}
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
        guard !apiChanging,!loginBusy,jelly.connected,catalogueReady else{log("WARN","surf","NOT_CONNECTED");return}
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
        guard !apiChanging,!loginBusy,jelly.connected,catalogueReady,policyReady else{return}
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
        guard !apiChanging,!loginBusy,jelly.connected,catalogueReady else{log("WARN","player","NOT_CONNECTED");return}
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
        guard !apiChanging,!loginBusy,jelly.connected,catalogueReady,policyReady else{log("WARN","player","NOT_CONNECTED");return}
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
        else if surfing {emptyPoolRetry=Date().addingTimeInterval(Double(policy.failureRetrySeconds))}
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
        processDecisions(now:now)
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
                if let c=current {
                    switch effectivePolicy.recoveryMode {
                    case "advance_surf":if surfing && !surfPaused{next()}else{begin(c,keepSurf:surfing)}
                    case "pause":pause();status="Playback paused by recovery policy"
                    default:begin(c,keepSurf:surfing)
                    }
                }
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
        if apiReady && !apiChanging && policyReady && (!jelly.connected || status=="Connection failed") && settings.autoLogin && settings.unattended && !settings.server.isEmpty && now.timeIntervalSince(lastLogin)>60 {Task{await connect()}}
        resumeCampaignIfNeeded()
        runModuleIfDue()
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
        do {try await uploadDecisions()}
        catch{log("WARN","decisions","UPLOAD_FAILED",["error":error.localizedDescription])}
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
        if let fault=jellyfinAPIFault{list.append(Insight(id:"jellyfin-api",priority:"High",title:"Jellyfin API adapter needs attention",explanation:fault,action:"Inspect API traces or restore the previous adapter."))}
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
        if moduleCheckpoint?.moduleID==activePatchID {
            list += (moduleCheckpoint?.insights ?? []).map{Insight(id:"module-"+$0.id,priority:$0.priority,title:$0.title,explanation:$0.explanation,action:$0.action)}
        }
        insights=list
        disk.save(list,"insights.json")
    }
    func stateSnapshot()->[String:Any]{
        ["schemaVersion":1,"deviceID":deviceID,"timestamp":ISO8601DateFormatter().string(from:Date()),"status":status,"currentChannel":current?.name ?? "","currentChannelID":current?.id ?? "","surfing":surfing,"surfPaused":surfPaused,"rotationMinutes":settings.rotationMinutes,"remainingSeconds":remaining,"channels":channels.count,"eligible":eligibleChannels.count,"hiatus":hiatus.count,"banned":banned.count,"playing":playing,"catalogueReady":catalogueReady,"lastPlayerError":lastPlayerError ?? "","metrics":metrics,"systemMetrics":systemMetrics,"telemetryActive":telemetryActive,"telemetryLastSample":telemetryLastSample.map{ISO8601DateFormatter().string(from:$0)} ?? "","launchID":launchID,"runtimePatch":activePatchID,"runtimeABI":1,"runtimeReady":policyReady,"jellyfinAPIABI":1,"jellyfinAPIRevision":jellyfinAPIRevision,"jellyfinAPIReady":apiReady,"jellyfinAPIFault":jellyfinAPIFault ?? "","uiABI":1,"uiRevision":dynamicUI.active?.revision ?? "native-baseline","uiPages":dynamicUI.active?.pages.map(\.id) ?? [],"pendingDecisions":decisions.records.filter{!$0.terminal}.count,"decisionABI":1,"campaignID":campaign?.plan.id ?? "","campaignState":campaign?.state ?? "idle","campaignCycle":campaign?.cycle ?? 0,"campaignStep":campaign?.stepIndex ?? 0,"insights":insights.map{["priority":$0.priority,"title":$0.title]},"actions":Self.actions]
    }
    static let actions=["get_status","list_channels","open_channel","open_server_surf","play","pause","resume","stop","next_channel","previous_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","favorite_current","favorite_previous","get_channel_health","list_banned","list_hiatus","approve_reinstatement","get_logs","get_telemetry","get_insights","get_capabilities","sync_now","reconnect","script","get_version","install_runtime_module","install_runtime_module_from_db","reload_runtime_module","rollback_runtime_module","reset_runtime_module","get_runtime_module","start_device_tests","pause_device_tests","resume_device_tests","stop_device_tests","get_device_tests","install_ui_layout","install_ui_layout_from_db","get_ui_layout","rollback_ui_layout","reset_ui_layout","ui_set_value","ui_trigger","ui_open_page","ui_close_page","ui_get_capabilities","present_decision","get_decisions","cancel_decision","get_jellyfin_api","install_jellyfin_api","install_jellyfin_api_from_db","rollback_jellyfin_api","reset_jellyfin_api","probe_jellyfin_api"]
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
            let structured:Set<String>=["get_status","get_capabilities","list_channels","get_channel_health","list_banned","list_hiatus","get_logs","get_telemetry","get_insights","get_runtime_module","get_device_tests","get_ui_layout","ui_get_capabilities","get_decisions","get_jellyfin_api","probe_jellyfin_api"]
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
        case "get_jellyfin_api":
            return try json(["revision":jellyfinAPIRevision,"ready":String(apiReady),"fault":jellyfinAPIFault ?? "","previous":jellyfinAPIStore.previous?.id ?? "","operations":JellyfinAdapterRules.operations.joined(separator:",")])
        case "install_jellyfin_api":
            guard let id=args["id"],let source=args["source"] else{throw AppError.message("API revision ID and source required")}
            try await activateJellyfinAPI(JellyfinAdapterRevision(id:id,source:source))
            return "Jellyfin API revision active: "+jellyfinAPIRevision
        case "install_jellyfin_api_from_db":
            guard let id=args["id"],DynamicUILayout.validID(id),let sha=args["sha"],sha.range(of:"^[0-9a-f]{40}$",options:.regularExpression) != nil,
                let file=try await db.read("engineering/jellyfin-api/"+id+".js"),file.sha==sha,file.data.count<=192*1024,
                let source=String(data:file.data,encoding:.utf8) else{throw AppError.message("API module source or exact blob SHA mismatch")}
            try await activateJellyfinAPI(JellyfinAdapterRevision(id:id,source:source))
            return "Jellyfin API revision active: "+jellyfinAPIRevision
        case "rollback_jellyfin_api":
            guard jellyfinAPIStore.active != nil else{throw AppError.message("Already using native API")}
            try await activateJellyfinAPI(jellyfinAPIStore.previous)
            return "Jellyfin API rolled back: "+jellyfinAPIRevision
        case "reset_jellyfin_api":
            try await activateJellyfinAPI(nil)
            return "Native Jellyfin API restored"
        case "probe_jellyfin_api":
            return try json(try await probeJellyfinAPI(args["operation"] ?? "health"))
        case "present_decision":
            guard let source=args["prompt"],source.utf8.count<=48*1024 else{throw AppError.message("Provide decision prompt JSON")}
            let prompt=try JSONCodec.decoder().decode(DecisionPrompt.self,from:Data(source.utf8))
            try queueDecision(prompt)
            return "Decision queued; awaiting local user choice: "+prompt.id
        case "get_decisions":
            if let id=args["id"]{
                guard let record=decisions.records.first(where:{$0.id==id}) else{throw AppError.message("Unknown decision")}
                return try json(record)
            }
            let summaries=decisions.records.suffix(50).map{["id":$0.id,"title":$0.prompt.title,"status":$0.status,"priority":$0.prompt.priority,"choice":$0.selectedChoiceID ?? ""]}
            return try json(Array(summaries))
        case "cancel_decision":
            guard let id=args["id"] else{throw AppError.message("Decision ID required")}
            try cancelDecision(id)
        case "install_ui_layout":
            guard let source=args["layout"],source.utf8.count<=192*1024 else{throw AppError.message("Provide layout JSON up to 192 KiB")}
            let layout=try JSONCodec.decoder().decode(DynamicUILayout.self,from:Data(source.utf8))
            try installUILayout(layout);return "UI layout activated: "+layout.revision
        case "install_ui_layout_from_db":
            guard let id=args["id"],DynamicUILayout.validID(id),let expected=args["sha"],
                  expected.range(of:"^[0-9a-f]{40}$",options:.regularExpression) != nil
            else{throw AppError.message("Layout revision and exact Git blob SHA required")}
            guard let file=try await db.read("engineering/ui/"+id+".json"),file.sha==expected,file.data.count<=192*1024
            else{throw AppError.message("Layout source missing or revision mismatch")}
            let layout=try JSONCodec.decoder().decode(DynamicUILayout.self,from:file.data)
            guard layout.revision==id else{throw AppError.message("Layout ID mismatch")}
            try installUILayout(layout);return "Repository UI layout activated: "+id
        case "get_ui_layout":return try json(dynamicUI.active)
        case "rollback_ui_layout":
            guard let previous=dynamicUI.previous else{throw AppError.message("No prior UI layout")}
            try commitUILayout(DynamicUIStore(active:previous,previous:dynamicUI.active,values:previous.formDefaults()))
            return "UI layout rolled back: "+previous.revision
        case "reset_ui_layout":
            try commitUILayout(DynamicUIStore(active:nil,previous:dynamicUI.active))
            return "Native UI restored"
        case "ui_set_value":
            guard let key=args["key"],let value=args["value"] else{throw AppError.message("Field key and value required")}
            try setDynamicUIValue(key,value:value)
        case "ui_trigger":
            guard let id=args["id"] else{throw AppError.message("UI node ID required")}
            let receipt=try await triggerDynamicUI(id,origin:"remote")
            guard receipt.status=="succeeded" else{throw AppError.message(receipt.result)}
            return receipt.result
        case "ui_open_page":
            guard let id=args["id"] else{throw AppError.message("Page ID required")}
            try openDynamicPage(id)
        case "ui_close_page":dynamicPage=nil
        case "ui_get_capabilities":
            return String(decoding:try JSONSerialization.data(withJSONObject:["abi":1,"locations":DynamicUILayout.locations,"kinds":DynamicUILayout.kinds,"commands":DynamicUILayout.commands.sorted()],options:[.sortedKeys]),as:UTF8.self)
        case "install_runtime_module":
            guard let id=args["id"],let source=args["source"] else{throw AppError.message("Module ID and JavaScript source required")}
            try await installRuntime(id:id,source:source);return "Runtime module activated: "+activePatchID
        case "install_runtime_module_from_db":
            guard let id=args["id"],id.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil,
                  let expected=args["sha"],expected.range(of:"^[0-9a-f]{40}$",options:.regularExpression) != nil
            else{throw AppError.message("Module revision ID and exact Git blob SHA required")}
            guard let file=try await db.read("engineering/modules/"+id+".js"),file.sha==expected,
                  file.data.count<=192*1024,let source=String(data:file.data,encoding:.utf8)
            else{throw AppError.message("Module source not found or blob revision mismatch")}
            try await installRuntime(id:id,source:source)
            return "Repository module activated: "+activePatchID
        case "reload_runtime_module":
            guard policyReady,!patchBusy else{throw AppError.message("Runtime busy")}
            await bootRuntime()
            if let fault=patchFault{throw AppError.message(fault)}
            return "Runtime reloaded: "+activePatchID
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
    func runScript(_ source:String,uiInputs:[String:String]=[:]) async throws -> String {
        let start=ProcessInfo.processInfo.systemUptime
        var input=stateSnapshot()
        input["form"]=uiInputs
        let (output,commands)=try await scripts.evaluateAsync(source,snapshot:input)
        var results=[sanitized(output)]
        for c in commands where c.action != "script" && c.action != "approve_reinstatement" && !["install_runtime_module","install_runtime_module_from_db","reload_runtime_module","rollback_runtime_module","reset_runtime_module","install_ui_layout","install_ui_layout_from_db","rollback_ui_layout","reset_ui_layout","ui_trigger","install_jellyfin_api","install_jellyfin_api_from_db","rollback_jellyfin_api","reset_jellyfin_api"].contains(c.action) {
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
        moduleTask?.cancel()
        policyOverride=policy
        activePatchID=engineering.activeID
        moduleFailures=0
        moduleEventCursor=nil
        if moduleCheckpoint?.moduleID != activePatchID {
            moduleCheckpoint=ModuleCheckpoint(moduleID:activePatchID)
            if let cp=moduleCheckpoint{disk.save(cp,"module-checkpoint.json")}
        }
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
        case "network":
            sampleSystemTelemetry(force:true)
            guard jelly.connected else{return Observation(status:"blocked",evidence:["reason":"Jellyfin not configured or connected"])}
            let ok=await jelly.healthy()
            return Observation(status:ok ? "passed":"failed",evidence:["networkState":networkState,"interfaces":networkInterfaces,"jellyfinReachable":String(ok),"requestMs":String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
        case "reconnect":
            await connect()
            return Observation(status:jelly.connected && catalogueReady ? "passed":"failed",evidence:["connected":String(jelly.connected),"catalogueReady":String(catalogueReady),"channelCount":String(channels.count)])
        case "audio_route":
            let audio=AVAudioSession.sharedInstance()
            let route=audio.currentRoute.outputs.map{$0.portType.rawValue}.joined(separator:",")
            return Observation(status:route.isEmpty ? "blocked":"passed",evidence:["outputRouteTypes":route,"sampleRate":String(audio.sampleRate),"ioBufferDuration":String(audio.ioBufferDuration),"outputLatency":String(audio.outputLatency),"outputChannels":String(audio.outputNumberOfChannels),"measurement":"Audio route only"])
        case "script_probe":
            guard let source=step.source else{return Observation(status:"failed",evidence:["reason":"Missing script probe"])}
            do {
                let (result,commands)=try await scripts.evaluateAsync(source,snapshot:stateSnapshot())
                guard commands.isEmpty else{return Observation(status:"failed",evidence:["reason":"Script probe cannot enqueue mutations"])}
                return Observation(status:result.trimmingCharacters(in:.whitespacesAndNewlines)=="true" ? "passed":"failed",evidence:["result":sanitized(result)])
            }catch{return Observation(status:"failed",evidence:["reason":error.localizedDescription])}
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
        maxTickGap=max(maxTickGap,finish-lastTick)
        guard generation==streamGeneration,current?.id==channel.id else{return Observation(status:"failed",evidence:["reason":"Playback changed before final observation"],channelID:channel.id)}
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
    private func runModuleIfDue(){
        guard policyReady,!patchBusy,activePatchID != "native-baseline",moduleTask==nil,moduleFailures<3,
              var cp=moduleCheckpoint,cp.moduleID==activePatchID,(cp.nextAt<=Date() || !(cp.pending ?? []).isEmpty) else{return}
        let module=activePatchID
        let newEvents:[Event]
        if let cursor=cp.eventCursor ?? moduleEventCursor,let i=events.firstIndex(where:{$0.id==cursor}){newEvents=Array(events.dropFirst(i+1).suffix(40))}
        else{newEvents=Array(events.suffix(40))}
        let cursor=newEvents.last?.id
        let eventData=(try? JSONCodec.encoder().encode(newEvents)) ?? Data("[]".utf8)
        let values=(try? JSONSerialization.jsonObject(with:eventData)) as? [[String:Any]] ?? []
        let snap=stateSnapshot()
        moduleTask=Task{[weak self] in
            guard let self=self else{return}
            defer{self.moduleTask=nil}
            do{
                if (cp.pending ?? []).isEmpty {
                    let decision=try await self.engineering.decision(snapshot:snap,state:cp.state,events:values,receipts:cp.lastReceipt)
                    guard !Task.isCancelled,self.activePatchID==module,!self.patchBusy else{return}
                    if let cursor=cursor{self.moduleEventCursor=cursor;cp.eventCursor=cursor}
                    cp.nextAt=Date().addingTimeInterval(Double(decision?.nextSeconds ?? 30))
                    cp.state=decision?.state ?? cp.state
                    cp.insights=(decision?.insights ?? cp.insights).map{Insight(id:$0.id,priority:$0.priority,title:self.sanitized($0.title),explanation:self.sanitized($0.explanation),action:self.sanitized($0.action))}
                    cp.lastReceipt=[]
                    cp.pending=(decision?.actions ?? []).map{Command(id:UUID().uuidString,action:$0.action,arguments:$0.arguments,expiresAt:nil)}
                    guard self.disk.save(cp,"module-checkpoint.json") else{throw AppError.message("Cannot journal module decision")}
                    self.moduleCheckpoint=cp
                }
                let allowed:Set<String>=["get_status","get_telemetry","get_insights","list_channels","get_channel_health","open_channel","play","pause","resume","stop","next_channel","start_surf","stop_surf","pause_surf","resume_surf","set_rotation_minutes","reconnect","sync_now","start_device_tests","pause_device_tests","resume_device_tests","stop_device_tests","get_device_tests","present_decision","get_decisions"]
                // Journal the actual command IDs before execution. After a crash
                // the ordinary command receipt prevents a duplicate mutation.
                while let command=cp.pending?.first {
                    guard !Task.isCancelled,self.activePatchID==module,!self.patchBusy else{return}
                    if !allowed.contains(command.action) {
                        cp.lastReceipt.append(command.action+": unsupported hook action")
                    } else if self.engineeringOwnsPlayback && !["get_status","get_telemetry","get_insights","get_device_tests","pause_device_tests","stop_device_tests","sync_now","present_decision","get_decisions"].contains(command.action) {
                        cp.lastReceipt.append(command.action+": campaign owns playback")
                    } else {
                        let result=await self.execute(command)
                        guard !Task.isCancelled,self.activePatchID==module,!self.patchBusy else{return}
                        if result.status=="running"{
                            cp.nextAt=Date().addingTimeInterval(5)
                            self.moduleCheckpoint=cp
                            self.disk.save(cp,"module-checkpoint.json")
                            return
                        }
                        cp.lastReceipt.append(command.action+": "+result.status+" "+String(result.result.prefix(2048)))
                    }
                    cp.pending?.removeFirst()
                    guard self.disk.save(cp,"module-checkpoint.json") else{throw AppError.message("Cannot checkpoint module action")}
                    self.moduleCheckpoint=cp
                }
                guard !Task.isCancelled,self.activePatchID==module,!self.patchBusy else{return}
                guard self.disk.save(cp,"module-checkpoint.json") else{throw AppError.message("Cannot save module state")}
                self.moduleCheckpoint=cp;self.moduleFailures=0
                self.recalculateInsights()
            }catch{
                guard self.activePatchID==module,!Task.isCancelled else{return}
                self.moduleFailures+=1
                cp.nextAt=Date().addingTimeInterval(30)
                self.moduleCheckpoint=cp
                self.disk.save(cp,"module-checkpoint.json")
                self.log("WARN","runtime","MODULE_TICK_FAILED",["module":module,"error":error.localizedDescription,"count":String(self.moduleFailures)])
                if self.moduleFailures>=3{self.patchFault="Module tick disabled after repeated errors; reload, replace or roll back";self.recalculateInsights()}
            }
        }
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
        if let module=moduleCheckpoint {
            let path="engineering/"+deviceID+"/module-state.json"
            let data=try JSONCodec.encoder().encode(module)
            let existing=try await db.read(path)
            if existing?.data != data{try await db.put(path,data:data,sha:existing?.sha)}
        }
        if let cp=campaign {
            let path="engineering/"+deviceID+"/checkpoint.json"
            let data=try JSONCodec.encoder().encode(cp)
            let existing=try await db.read(path)
            if existing?.data != data {try await db.put(path,data:data,sha:existing?.sha)}
        }
    }
}


// Remote declarative UI. Native screens and their recovery controls remain available.
extension AppModel {
    func dynamicValue(_ key:String?) -> String? {
        guard let key=key else{return nil}
        if key.hasPrefix("form.") {return dynamicUI.values[String(key.dropFirst(5))]}
        return DynamicUIBinding.resolve(key,values:dynamicUI.values,state:stateSnapshot())
    }
    func dynamicVisible(_ node:DynamicUINode)->Bool{
        guard let key=node.visibleWhen else{return true}
        return DynamicUIBinding.truth(dynamicValue(key))
    }
    func dynamicEnabled(_ node:DynamicUINode)->Bool{
        guard let key=node.enabledWhen else{return true}
        return DynamicUIBinding.truth(dynamicValue(key))
    }
    private func commitUILayout(_ store:DynamicUIStore)throws {
        guard dynamicUIBusy.isEmpty else{throw AppError.message("A UI action is still executing")}
        if let layout=store.active{_ = try layout.checked()}
        guard disk.save(store,"dynamic-ui.json") else{throw AppError.message("Cannot persist UI layout")}
        dynamicPage=nil
        dynamicUIResults=[:]
        dynamicUISeen.removeAll()
        dynamicUI=store
        log("INFO","dynamic-ui","LAYOUT_CHANGED",["revision":store.active?.revision ?? "native-baseline","slots":String(store.active?.slots.count ?? 0),"pages":String(store.active?.pages.count ?? 0)])
        requestSync()
    }
    private func installUILayout(_ layout:DynamicUILayout)throws {
        let layout=try layout.checked()
        guard layout.revision != dynamicUI.active?.revision else{throw AppError.message("Use a new UI revision")}
        let values=layout.retainedValues(dynamicUI.values)
        try commitUILayout(DynamicUIStore(active:layout,previous:dynamicUI.active,values:values))
    }
    func setDynamicUIValue(_ key:String,value:String)throws{
        guard let layout=dynamicUI.active else{throw AppError.message("No active UI layout")}
        let field=key.hasPrefix("form.") ? String(key.dropFirst(5)):key
        guard let node=layout.formNode(field) else{throw AppError.message("Unknown form field")}
        try DynamicUILayout.validateValue(value,for:node)
        guard dynamicUI.values[field] != value else{return}
        var copy=dynamicUI;copy.values[field]=value
        guard disk.save(copy,"dynamic-ui.json") else{throw AppError.message("Cannot persist form value")}
        dynamicUI=copy
    }
    func setDynamicUIValueFromView(_ key:String,value:String){
        do{try setDynamicUIValue(key,value:value)}
        catch{log("WARN","dynamic-ui","FIELD_WRITE_FAILED",["field":key,"error":error.localizedDescription])}
    }
    func openDynamicPage(_ id:String)throws{
        guard let page=dynamicUI.active?.pages.first(where:{$0.id==id}) else{throw AppError.message("Unknown dynamic page")}
        dynamicPage=page
        log("INFO","dynamic-ui","PAGE_OPENED",["page":id,"revision":dynamicUI.active?.revision ?? ""])
    }
    func openDynamicPageFromView(_ id:String){
        do{try openDynamicPage(id)}
        catch{log("WARN","dynamic-ui","PAGE_OPEN_FAILED",["page":id])}
    }
    func observeDynamicSlot(_ slot:String){
        let identity=(dynamicUI.active?.revision ?? "")+":"+slot
        guard dynamicUISeen.insert(identity).inserted else{return}
        log("INFO","dynamic-ui","SLOT_RENDERED",["slot":slot,"revision":dynamicUI.active?.revision ?? ""])
    }
    func triggerDynamicUI(_ id:String,origin:String)async throws->Receipt{
        guard let layout=dynamicUI.active,let node=layout.node(id),let action=node.action,
              dynamicVisible(node),dynamicEnabled(node) else{throw AppError.message("UI action unavailable")}
        guard dynamicUIBusy.insert(id).inserted else{throw AppError.message("UI action already running")}
        defer{dynamicUIBusy.remove(id)}
        let inputs=dynamicUI.values
        let args=try DynamicUIBinding.arguments(action.arguments ?? [:],values:inputs,state:stateSnapshot())
        log("INFO","dynamic-ui","ACTION_STARTED",["node":id,"origin":origin,"revision":layout.revision,"command":action.command])
        let result:Receipt
        if action.command=="script" {
            do {
                guard let source=args["source"] else{throw AppError.message("Missing script source")}
                let output=try await runScript(source,uiInputs:inputs)
                result=Receipt(id:UUID().uuidString,action:"script",status:"succeeded",result:output)
            }catch{
                result=Receipt(id:UUID().uuidString,action:"script",status:"failed",result:sanitized(error.localizedDescription))
            }
        }else{
            result=await execute(Command(id:UUID().uuidString,action:action.command,arguments:args,expiresAt:nil))
        }
        dynamicUIResults[id]=sanitized(result.result,limit:8192)
        log(result.status=="succeeded" ? "INFO":"WARN","dynamic-ui","ACTION_FINISHED",["node":id,"origin":origin,"revision":layout.revision,"status":result.status,"receipt":result.id])
        requestSync()
        return result
    }
    func executeWebUI(nodeID:String,revision:String,action:DynamicUIAction) async -> Receipt {
        guard dynamicUI.active?.revision==revision,let node=dynamicUI.active?.node(nodeID),node.kind=="web",
            dynamicVisible(node),dynamicEnabled(node),DynamicUILayout.commands.contains(action.command),
            action.command != "script",(action.arguments?.count ?? 0)<=32,
            (action.arguments ?? [:]).allSatisfy({$0.key.count<=128 && $0.value.utf8.count<=65536})
        else{return Receipt(id:UUID().uuidString,action:action.command,status:"failed",result:"Web component or command unavailable")}
        guard dynamicUIBusy.insert(nodeID).inserted else{return Receipt(id:UUID().uuidString,action:action.command,status:"running",result:"Component busy")}
        defer{dynamicUIBusy.remove(nodeID)}
        do{
            let args=try DynamicUIBinding.arguments(action.arguments ?? [:],values:dynamicUI.values,state:stateSnapshot())
            log("INFO","dynamic-ui","WEB_ACTION_STARTED",["node":nodeID,"revision":revision,"command":action.command])
            let receipt=await execute(Command(id:UUID().uuidString,action:action.command,arguments:args,expiresAt:nil))
            log(receipt.status=="succeeded" ? "INFO":"WARN","dynamic-ui","WEB_ACTION_FINISHED",["node":nodeID,"revision":revision,"status":receipt.status,"receipt":receipt.id])
            requestSync()
            return receipt
        }catch{return Receipt(id:UUID().uuidString,action:action.command,status:"failed",result:sanitized(error.localizedDescription))}
    }
    func triggerDynamicUIFromView(_ id:String){
        Task{
            do{_ = try await triggerDynamicUI(id,origin:"touch")}
            catch{
                dynamicUIResults[id]=sanitized(error.localizedDescription)
                log("WARN","dynamic-ui","ACTION_FAILED",["node":id,"error":error.localizedDescription])
            }
        }
    }
}


// User decisions are separate from the remote command mailbox. Only the
// native prompt interaction can select a choice. Remote cancellation is not approval.
extension AppModel {
    var activeDecision:DecisionRecord? {decisions.next(at:Date())}
    private func saveDecisions(_ journal:DecisionJournal)throws{
        guard disk.save(journal,"decision-journal.json") else{throw AppError.message("Cannot persist decision journal")}
        decisions=journal
    }
    private func queueDecision(_ prompt:DecisionPrompt)throws{
        let existed=decisions.records.contains(where:{$0.id==prompt.id})
        var copy=decisions
        try copy.insert(prompt,at:Date())
        guard !existed else{return}
        try saveDecisions(copy)
        log("INFO","decisions","PROMPT_QUEUED",["id":prompt.id,"priority":prompt.priority])
        requestSync()
    }
    private func cancelDecision(_ id:String)throws{
        var copy=decisions
        guard let i=copy.records.firstIndex(where:{$0.id==id}) else{throw AppError.message("Unknown decision")}
        try copy.records[i].cancel(at:Date())
        try saveDecisions(copy)
        log("INFO","decisions","PROMPT_CANCELLED",["id":id,"origin":"remote"])
        requestSync()
    }
    func decisionPresented(_ id:String){
        var copy=decisions
        guard let i=copy.records.firstIndex(where:{$0.id==id}),copy.records[i].presentedAt==nil,
              activeDecision?.id==id else{return}
        copy.records[i].present(at:Date())
        guard copy.records[i].presentedAt != nil else{return}
        do{
            try saveDecisions(copy)
            log("INFO","decisions","PROMPT_PRESENTED",["id":id,"tab":String(tab),"page":dynamicPage?.id ?? ""])
            requestSync()
        }catch{log("ERROR","decisions","PRESENTATION_PERSIST_FAILED",["id":id])}
    }
    func answerDecision(_ id:String,choice:String)throws{
        var copy=decisions
        guard activeDecision?.id==id,let i=copy.records.firstIndex(where:{$0.id==id}) else{throw AppError.message("Prompt no longer active")}
        // Persist the exact choice before any effect, then execute via the
        // regular durable command receipt with a stable command ID.
        try copy.records[i].choose(choice,at:Date())
        try saveDecisions(copy)
        log("INFO","decisions","USER_ANSWERED",["id":id,"choice":choice,"status":copy.records[i].status,"origin":"touch"])
        requestSync()
        processDecisions(now:Date())
    }
    private func processDecisions(now:Date){
        var copy=decisions
        var changed=false
        for i in copy.records.indices where copy.records[i].status=="pending" && copy.records[i].prompt.expiresAt<=now {
            copy.records[i].expire(at:now);changed=true
        }
        if changed {
            do{try saveDecisions(copy);requestSync()}
            catch{log("ERROR","decisions","EXPIRY_PERSIST_FAILED");return}
        }
        guard decisionTask==nil,policyReady,!patchBusy,
            let record=decisions.records.first(where:{$0.status=="approved"}),
            let action=record.chosenAction else{return}
        decisionTask=Task{[weak self] in
            guard let self=self else{return}
            defer{self.decisionTask=nil}
            let receipt:Receipt?
            // An interrupted process might have applied the command before
            // persisting the decision outcome. Recover its original receipt.
            if let existing=self.disk.load(Receipt.self,"receipt-\(record.commandID).json"){
                receipt=existing
            }else if record.prompt.expiresAt<=Date() ||
                !record.prompt.contextMatches(channelID:self.current?.id,patchID:self.activePatchID,apiRevision:self.jellyfinAPIRevision){
                receipt=nil
            }else{
                receipt=await self.execute(Command(id:record.commandID,action:action.command,arguments:action.arguments,expiresAt:record.prompt.expiresAt))
            }
            guard var latest=self.decisions.records.first(where:{$0.id==record.id}),latest.status=="approved" else{return}
            if let receipt=receipt{
                if receipt.status=="running"{return}
                latest.finish(receipt,at:Date())
            }else{latest.stale(at:Date())}
            var journal=self.decisions
            guard let i=journal.records.firstIndex(where:{$0.id==record.id}) else{return}
            journal.records[i]=latest
            do{
                try self.saveDecisions(journal)
                self.log(latest.status=="succeeded" ? "INFO":"WARN","decisions","DECISION_COMPLETED",["id":latest.id,"status":latest.status,"commandID":record.commandID])
                self.requestSync()
            }catch{self.log("ERROR","decisions","OUTCOME_PERSIST_FAILED",["id":record.id])}
        }
    }
    private func uploadDecisions() async throws{
        // Each uploaded revision is an immutable snapshot. Local updates during
        // network awaits are not overwritten by an older upload acknowledgement.
        for record in decisions.pendingUpload.prefix(12){
            let path="engineering/"+deviceID+"/decisions/"+record.id+"/revision-"+String(record.revision)+".json"
            let data=try JSONCodec.encoder().encode(record)
            try await db.immutable(path,data:data)
            var copy=decisions
            copy.markUploaded(id:record.id,revision:record.revision)
            try saveDecisions(copy)
        }
    }
}


extension AppModel {
    private func persistJellyfinAPI(_ store:JellyfinAdapterStore)throws{
        guard disk.save(store,"jellyfin-api.json") else{throw AppError.message("Cannot persist Jellyfin API revision")}
        jellyfinAPIStore=store
    }
    private func bootJellyfinAPI()async{
        apiChanging=true
        defer{apiReady=true;apiChanging=false}
        do{
            if jellyfinAPIStore.pending {
                try persistJellyfinAPI(JellyfinAdapterStore(active:jellyfinAPIStore.previous,previous:nil,pending:false))
                jellyfinAPIFault="Interrupted API activation rolled back"
            }
            if let revision=jellyfinAPIStore.active{
                do{
                    jelly.adapter=try await JellyfinAPIAdapter.load(revision,engine:scripts)
                }catch{
                    let fallback=jellyfinAPIStore.previous
                    if let fallback=fallback,let loaded=try? await JellyfinAPIAdapter.load(fallback,engine:scripts){
                        jelly.adapter=loaded
                        try persistJellyfinAPI(JellyfinAdapterStore(active:fallback,previous:nil,pending:false))
                    }else{
                        jelly.adapter=nil
                        try persistJellyfinAPI(JellyfinAdapterStore(active:nil,previous:nil,pending:false))
                    }
                    jellyfinAPIFault="Persisted API module rejected at launch; fallback restored"
                }
            }
        }catch{jelly.adapter=nil;jellyfinAPIFault="API restoration failed; native fallback in use"}
        jellyfinAPIRevision=jelly.adapterID
        log(jellyfinAPIFault==nil ? "INFO":"WARN","jellyfin-api","RESTORED",["revision":jellyfinAPIRevision,"fault":jellyfinAPIFault ?? ""])
        recalculateInsights()
    }
    private func activateJellyfinAPI(_ candidate:JellyfinAdapterRevision?)async throws{
        guard apiReady,!apiChanging,!loginBusy,!catalogueBusy,favoriteInFlight.isEmpty,!patchBusy else{throw AppError.message("Jellyfin adapter busy; retry after current operation")}
        guard candidate?.id != jellyfinAPIStore.active?.id || candidate==nil else{throw AppError.message("Use a new API revision ID")}
        apiChanging=true
        defer{apiChanging=false;resumeCampaignIfNeeded();requestSync()}
        let previous=jellyfinAPIStore
        let resumeLoop=surfing || settings.resumeSurf
        let category=settings.surfCategory
        let channelID=current?.id
        do {
            // A candidate must pass interpreter fixtures and real read-only
            // catalogue/health probes before replacing the active adapter.
            let loaded:JellyfinAPIAdapter?
            if let candidate=candidate{loaded=try await JellyfinAPIAdapter.load(candidate,engine:scripts)}
            else{loaded=nil}
            let staged=Jellyfin(deviceID:deviceID)
            staged.adapter=loaded
            staged.log={ [weak self] severity,code,fields in self?.log(severity,"jellyfin-api-stage",code,fields)}
            try await staged.connect(settings)
            guard await staged.healthy() else{throw AppError.message("Candidate health probe failed")}
            let catalogue=try await staged.channels()
            guard !catalogue.isEmpty else{throw AppError.message("Candidate returned an empty live catalogue")}
            let epg=try? await staged.programs()
            try suspendCampaignForPatch()
            let oldWork=work
            stop()
            await oldWork?.value
            await cleanupTask?.value
            let deadline=ProcessInfo.processInfo.systemUptime+30
            while jelly.inFlight>0 && ProcessInfo.processInfo.systemUptime<deadline{await nap(0.2)}
            guard jelly.inFlight==0 else{throw AppError.message("Old Jellyfin operations have not drained")}
            try persistJellyfinAPI(JellyfinAdapterStore(active:candidate,previous:previous.active,pending:false))
            jelly.adapter=loaded;jelly.base=staged.base;jelly.token=staged.token;jelly.userID=staged.userID
            jellyfinAPIRevision=jelly.adapterID;jellyfinAPIFault=nil
            channels=catalogue;programs=epg ?? [];catalogueReady=true;lastRefresh=Date()
            disk.save(catalogue,"channels.json")
            status="Connected"
            log("INFO","jellyfin-api","ACTIVATED",["revision":jellyfinAPIRevision,"channels":String(catalogue.count),"epgProbe":epg==nil ? "unavailable":"passed"])
            apiChanging=false
            if !engineeringOwnsPlayback && campaign?.canResume != true{
                if resumeLoop{startSurf(category:category)}
                else if let id=channelID,let c=channels.first(where:{$0.id==id}),health[id]?.eligible(at:Date()) ?? true{open(c)}
            }
            recalculateInsights()
        }catch{
            jellyfinAPIFault="Candidate API revision rejected; previous adapter retained"
            log("ERROR","jellyfin-api","ACTIVATION_REJECTED",["candidate":candidate?.id ?? "native-baseline","errorType":String(describing:type(of:error))])
            apiChanging=false
            if current==nil,jelly.connected,catalogueReady,campaign?.canResume != true{
                if resumeLoop{startSurf(category:category)}
                else if let id=channelID,let c=channels.first(where:{$0.id==id}){open(c)}
            }
            recalculateInsights()
            throw AppError.message(jellyfinAPIFault ?? "API activation failed")
        }
    }
    private func probeJellyfinAPI(_ operation:String)async throws->[String:String]{
        guard apiReady,!apiChanging,jelly.connected else{throw AppError.message("Jellyfin API unavailable")}
        let start=ProcessInfo.processInfo.systemUptime
        var result=["revision":jellyfinAPIRevision,"operation":operation]
        switch operation{
        case "health":guard await jelly.healthy() else{throw AppError.message("Health probe failed")}
        case "channels":result["count"]=String(try await jelly.channels().count)
        case "programs":result["count"]=String(try await jelly.programs().count)
        case "recordings":result["count"]=String(try await jelly.recordings().count)
        default:throw AppError.message("Unsupported read-only probe")
        }
        result["elapsedMs"]=String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))
        result["status"]="passed"
        log("INFO","jellyfin-api","PROBE_COMPLETED",result)
        return result
    }
}
