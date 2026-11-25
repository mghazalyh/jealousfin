import Foundation

struct RuntimePolicy: Codable, Equatable {
    var startupTimeout: Int
    var retries: Int
    var qualityMbps: Int
    var stallSeconds: Int
    var rotationSeconds: Int
    var validationSeconds: Int
    var minimumFrames: Int
    var forwardBufferSeconds: Int
    var automaticallyWaits: Bool
    var recoveryMode: String
    var failureRetrySeconds: Int
    var playback:PlaybackAttemptPolicy? = nil
    var strictChannelHiatus:Bool? = nil
    static func baseline(_ settings: Settings) -> RuntimePolicy {
        RuntimePolicy(startupTimeout: settings.startupTimeout, retries: settings.retries,
            qualityMbps: settings.qualityMbps, stallSeconds: 15, rotationSeconds: settings.rotationMinutes * 60,
            validationSeconds: 3, minimumFrames: 3, forwardBufferSeconds: 0, automaticallyWaits: true, recoveryMode:"advance_surf", failureRetrySeconds:30, strictChannelHiatus:true)
    }
    func checked() throws -> RuntimePolicy {
        guard (5...120).contains(startupTimeout), (1...5).contains(retries), (1...50).contains(qualityMbps),
            (5...300).contains(stallSeconds), (5...86400).contains(rotationSeconds),
            (3...60).contains(validationSeconds), (3...120).contains(minimumFrames),
            (0...120).contains(forwardBufferSeconds), startupTimeout > validationSeconds,
            ["reopen","advance_surf","pause"].contains(recoveryMode),(5...600).contains(failureRetrySeconds)
        else { throw AppError.message("Runtime policy is outside supported native limits") }
        _ = try playback?.checked()
        return self
    }
}
struct PlaybackAttemptPolicy:Codable,Equatable {
    var segmentContainer:String = "server"
    var forceEncode:Bool = false
    var segmentSeconds:Int = 0
    var minimumSegments:Int = 0
    var maxWidth:Int = 0
    var maxHeight:Int = 0
    var videoBitrate:Int = 0
    var audioBitrate:Int = 0
    var peakMbps:Int = 0
    var forwardBufferSeconds:Int? = nil
    var automaticallyWaits:Bool? = nil
    var startupSeconds:Int? = nil
    var diagnosticSeconds:Int = 0
    var preferDirect:Bool = false
    var preferredMaximumWidth:Int? = nil
    var preferredMaximumHeight:Int? = nil
    var networkWhilePaused:Bool? = nil
    init(){}
    init(from decoder:Decoder)throws{
        let c=try decoder.container(keyedBy:CodingKeys.self)
        segmentContainer=try c.decodeIfPresent(String.self,forKey:.segmentContainer) ?? "server"
        forceEncode=try c.decodeIfPresent(Bool.self,forKey:.forceEncode) ?? false
        segmentSeconds=try c.decodeIfPresent(Int.self,forKey:.segmentSeconds) ?? 0
        minimumSegments=try c.decodeIfPresent(Int.self,forKey:.minimumSegments) ?? 0
        maxWidth=try c.decodeIfPresent(Int.self,forKey:.maxWidth) ?? 0
        maxHeight=try c.decodeIfPresent(Int.self,forKey:.maxHeight) ?? 0
        videoBitrate=try c.decodeIfPresent(Int.self,forKey:.videoBitrate) ?? 0
        audioBitrate=try c.decodeIfPresent(Int.self,forKey:.audioBitrate) ?? 0
        peakMbps=try c.decodeIfPresent(Int.self,forKey:.peakMbps) ?? 0
        forwardBufferSeconds=try c.decodeIfPresent(Int.self,forKey:.forwardBufferSeconds)
        automaticallyWaits=try c.decodeIfPresent(Bool.self,forKey:.automaticallyWaits)
        startupSeconds=try c.decodeIfPresent(Int.self,forKey:.startupSeconds)
        diagnosticSeconds=try c.decodeIfPresent(Int.self,forKey:.diagnosticSeconds) ?? 0
        preferDirect=try c.decodeIfPresent(Bool.self,forKey:.preferDirect) ?? false
        preferredMaximumWidth=try c.decodeIfPresent(Int.self,forKey:.preferredMaximumWidth)
        preferredMaximumHeight=try c.decodeIfPresent(Int.self,forKey:.preferredMaximumHeight)
        networkWhilePaused=try c.decodeIfPresent(Bool.self,forKey:.networkWhilePaused)
    }
    func checked()throws->Self {
        guard ["server","ts","mp4"].contains(segmentContainer),
              (0...15).contains(segmentSeconds),(0...8).contains(minimumSegments),
              (0...3840).contains(maxWidth),(0...2160).contains(maxHeight),
              (0...50_000_000).contains(videoBitrate),(0...512_000).contains(audioBitrate),
              (0...50).contains(peakMbps),(0...120).contains(forwardBufferSeconds ?? 0),
              (5...120).contains(startupSeconds ?? 25),(0...20).contains(diagnosticSeconds),
              (0...3840).contains(preferredMaximumWidth ?? 0),(0...2160).contains(preferredMaximumHeight ?? 0),
              ((preferredMaximumWidth ?? 0)==0)==((preferredMaximumHeight ?? 0)==0)
        else{throw AppError.message("Playback attempt policy outside supported limits")}
        return self
    }
    func applying(to url:URL)throws->URL {
        guard var c=URLComponents(url:url,resolvingAgainstBaseURL:false) else{throw AppError.message("Invalid playback URL")}
        // Only ordinary HLS paths receive format overrides.
        guard c.path.lowercased().hasSuffix(".m3u8") else{return url}
        var changes:[String:String]=[:]
        if segmentContainer != "server"{changes["SegmentContainer"]=segmentContainer}
        if forceEncode{for k in ["EnableAutoStreamCopy","AllowVideoStreamCopy","AllowAudioStreamCopy"]{changes[k]="false"}}
        for (key,value) in [("SegmentLength",segmentSeconds),("MinSegments",minimumSegments),("MaxWidth",maxWidth),("MaxHeight",maxHeight),("VideoBitRate",videoBitrate),("AudioBitRate",audioBitrate)] where value>0 {changes[key]=String(value)}
        guard !changes.isEmpty else{return url}
        let keys=Set(changes.keys.map{$0.lowercased()})
        c.queryItems=(c.queryItems ?? []).filter{!keys.contains($0.name.lowercased())}+changes.sorted{$0.key<$1.key}.map{URLQueryItem(name:$0.key,value:$0.value)}
        guard let result=c.url else{throw AppError.message("Invalid playback format override")}
        return result
    }
}
struct RuntimePatch: Codable, Equatable {
    var id: String
    var source: String
    var installedAt = Date()
}
struct RuntimePatchStore: Codable {
    var active: RuntimePatch?
    var previous: RuntimePatch?
    var activationPending = false
}
struct DeviceTestStep: Codable, Equatable {
    var kind: String
    var channelID: String?
    var seconds: Int?
    var source: String?
}
struct DeviceTestPlan: Codable, Equatable {
    var id: String
    var steps: [DeviceTestStep]
    var repeatCount: Int = 1
    var repeatForever: Bool = false
    func checked() throws -> DeviceTestPlan {
        guard id.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil,
              (1...200).contains(steps.count), (1...10000).contains(repeatCount)
        else { throw AppError.message("Invalid test plan ID, step count or repeats") }
        for s in steps {
            guard ["system","catalogue","playback","pause_resume","endurance","surf_rotation","shared_surf_continuity","background","stop","network","audio_route","reconnect","script_probe"].contains(s.kind),
                  (1...3600).contains(s.seconds ?? 30) else {throw AppError.message("Invalid test step")}
            if s.kind=="script_probe" {
                guard let source=s.source,!source.isEmpty,source.utf8.count<=24000 else{throw AppError.message("Invalid script probe")}
            }
        }
        return self
    }
}
struct DeviceTestResult: Codable, Identifiable {
    var id = UUID().uuidString
    var planID: String
    var cycle: Int
    var stepIndex: Int
    var kind: String
    var channelID: String?
    var startedAt: Date
    var finishedAt = Date()
    var status: String
    var evidence: [String:String]
    var launchID: String
    var patchID: String
}
struct DeviceTestCheckpoint: Codable {
    var plan: DeviceTestPlan
    var runID = UUID().uuidString
    var cycle = 0
    var stepIndex = 0
    var state = "queued"
    var updatedAt = Date()
    var lastResult: DeviceTestResult?
    var resultID:String {"\(runID)-\(cycle)-\(stepIndex)"}
    var canResume:Bool {["queued","running"].contains(state) && !plan.steps.isEmpty && stepIndex>=0 && stepIndex<plan.steps.count && cycle>=0}
    mutating func advance(_ result: DeviceTestResult) {
        guard result.planID==plan.id,result.cycle==cycle,result.stepIndex==stepIndex else{return}
        lastResult=result
        stepIndex+=1
        if stepIndex>=plan.steps.count {
            cycle+=1;stepIndex=0
            if !plan.repeatForever && cycle>=plan.repeatCount {state="completed"}
        }
        updatedAt=Date()
    }
}

struct ModuleAction: Codable {
    var action: String
    var arguments: [String:String]?
}
struct ModuleDecision: Codable {
    var state: [String:String]?
    var actions: [ModuleAction]?
    var nextSeconds: Int?
    var insights: [Insight]?
    func checked() throws -> ModuleDecision {
        guard (state?.count ?? 0)<=128,
            (state ?? [:]).allSatisfy({$0.key.utf8.count<=128 && $0.value.utf8.count<=4096}),
            (actions?.count ?? 0)<=8,
            (5...3600).contains(nextSeconds ?? 15),
            (insights?.count ?? 0)<=20,
            (insights ?? []).allSatisfy({["Emergency","High","Attention","Informational","Resolved"].contains($0.priority) && $0.id.count<=128 && $0.title.count<=512 && $0.explanation.count<=4096 && $0.action.count<=2048})
        else {throw AppError.message("Module decision exceeds supported bounds")}
        return self
    }
}
struct ModuleCheckpoint: Codable {
    var moduleID:String
    var state:[String:String] = [:]
    var nextAt=Date()
    var insights:[Insight] = []
    var lastReceipt:[String] = []
    var eventCursor:String?
    var pending:[Command]?
}

// Configuration is data, not executable code. All fields are independently
// optional except identity, so older files survive future schema additions.
struct HotConfiguration:Codable,Equatable {
    var schemaVersion:Int=1
    var revision:String
    var runtime:RuntimePolicy?=nil
    var playbackRules:[ConfiguredPlaybackRule]?=nil
    var operations:OperationalConfiguration?=nil
    var interpreter:InterpreterBudget?=nil
    var presentation:DynamicPresentation?=nil
    var gateway:GatewayConfiguration?=nil
    static let baseline=HotConfiguration(revision:"native-baseline")
    func checked()throws->Self{
        guard schemaVersion==1,revision.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil,
              (playbackRules?.count ?? 0)<=64 else{throw AppError.message("Invalid configuration schema or rule count")}
        _ = try runtime?.checked()
        _ = try operations?.checked()
        _ = try interpreter?.checked()
        _ = try presentation?.checked();_ = try gateway?.checked()
        for rule in playbackRules ?? []{_ = try rule.checked()}
        return self
    }
    func attemptPolicy(channelID:String,attempt:Int,defaults:PlaybackAttemptPolicy)->PlaybackAttemptPolicy{
        // Ordered last match wins; exact IDs can override a broad fallback.
        var p=defaults
        for rule in playbackRules ?? [] where rule.matches(channelID:channelID,attempt:attempt){p=rule.policy}
        return p
    }
    static func decode(_ data:Data)throws->Self{
        guard data.count<=192*1024 else{throw AppError.message("Configuration exceeds size limit")}
        guard let root=try JSONSerialization.jsonObject(with:data) as? [String:Any] else{throw AppError.message("Expected configuration object")}
        // Fail on typos and unimplemented controls; do not silently accept no-ops.
        func keys(_ object:[String:Any],_ permitted:Set<String>)throws{
            guard Set(object.keys).isSubset(of:permitted) else{throw AppError.message("Unknown configuration field")}
        }
        try keys(root,["schemaVersion","revision","runtime","playbackRules","operations","interpreter","presentation","gateway"])
        let playbackKeys:Set<String>=["segmentContainer","forceEncode","segmentSeconds","minimumSegments","maxWidth","maxHeight","videoBitrate","audioBitrate","peakMbps","forwardBufferSeconds","automaticallyWaits","startupSeconds","diagnosticSeconds","preferDirect","preferredMaximumWidth","preferredMaximumHeight","networkWhilePaused"]
        if let runtime=root["runtime"] as? [String:Any]{
            try keys(runtime,["startupTimeout","retries","qualityMbps","stallSeconds","rotationSeconds","validationSeconds","minimumFrames","forwardBufferSeconds","automaticallyWaits","recoveryMode","failureRetrySeconds","playback","strictChannelHiatus"])
            if let p=runtime["playback"] as? [String:Any]{try keys(p,playbackKeys)}
        }
        for rule in root["playbackRules"] as? [[String:Any]] ?? [] {
            try keys(rule,["channelID","attempt","policy"])
            if let p=rule["policy"] as? [String:Any]{try keys(p,playbackKeys)}
        }
        if let op=root["operations"] as? [String:Any]{
            try keys(op,["systemSampleSeconds","playbackSampleSeconds","heartbeatSeconds","syncSeconds","catalogueRefreshSeconds","reconnectSeconds","eventRetentionCount","eventRetentionMiB","resourceWarningMiB","suspendAutomationOnPressure","playerEventDiagnostics","playerEventSampleSeconds","surfTransport"])
        }
        if let budget=root["interpreter"] as? [String:Any]{try keys(budget,["timeoutMs","memoryMiB","maxCommands"])}
        if let ui=root["presentation"] as? [String:Any]{try keys(ui,["videoGravity","fullscreenOpacity","fullscreenMaxHeight","fullscreenInitiallyVisible","nativePlaybackControls","pictureInPictureAllowed","automaticPictureInPicture"])}
        if let gateway=root["gateway"] as? [String:Any]{try keys(gateway,["lanServerPort","lanGatewayPort","routePrefix","ticketField","timeoutSeconds"])}
        var normalized=root
        if let runtime=root["runtime"] as? [String:Any]{
            let defaults=try JSONCodec.encoder().encode(RuntimePolicy.baseline(Settings()))
            guard let native=try JSONSerialization.jsonObject(with:defaults) as? [String:Any] else{throw AppError.message("Cannot compose runtime configuration")}
            normalized["runtime"]=native.merging(runtime){_,new in new}
        }
        return try JSONCodec.decoder().decode(Self.self,from:JSONSerialization.data(withJSONObject:normalized)).checked()
    }
}
struct ConfiguredPlaybackRule:Codable,Equatable{
    var channelID:String?=nil
    var attempt:Int?=nil
    var policy:PlaybackAttemptPolicy
    func checked()throws->Self{
        guard channelID.map({!$0.isEmpty && $0.utf8.count<=128}) ?? true,
              (1...5).contains(attempt ?? 1) else{throw AppError.message("Invalid playback rule match")}
        _ = try policy.checked()
        return self
    }
    func matches(channelID:String,attempt:Int)->Bool{
        (self.channelID==nil || self.channelID==channelID) && (self.attempt==nil || self.attempt==attempt)
    }
}
struct OperationalConfiguration:Codable,Equatable{
    var systemSampleSeconds:Int?=nil
    var playbackSampleSeconds:Int?=nil
    var heartbeatSeconds:Int?=nil
    var syncSeconds:Int?=nil
    var catalogueRefreshSeconds:Int?=nil
    var reconnectSeconds:Int?=nil
    var eventRetentionCount:Int?=nil
    var eventRetentionMiB:Int?=nil
    var resourceWarningMiB:Int?=nil
    var suspendAutomationOnPressure:Bool?=nil
    var playerEventDiagnostics:Bool?=nil
    var playerEventSampleSeconds:Int?=nil
    var surfTransport:String?=nil
    func checked()throws->Self{
        guard (5...300).contains(systemSampleSeconds ?? 15),
              (5...120).contains(playbackSampleSeconds ?? 15),
              (15...600).contains(heartbeatSeconds ?? 30),
              (15...600).contains(syncSeconds ?? 30),
              (60...3600).contains(catalogueRefreshSeconds ?? 180),
              (15...600).contains(reconnectSeconds ?? 60),
              (500...20000).contains(eventRetentionCount ?? 5000),
              (4...128).contains(eventRetentionMiB ?? 32),
              (64...2048).contains(resourceWarningMiB ?? 512),
              (1...60).contains(playerEventSampleSeconds ?? 5),
              ["auto","device"].contains(surfTransport ?? "auto")
        else{throw AppError.message("Operational configuration outside supported bounds")}
        return self
    }
}
struct InterpreterBudget:Codable,Equatable{
    var timeoutMs:Int?=nil
    var memoryMiB:Int?=nil
    var maxCommands:Int?=nil
    func checked()throws->Self{
        guard (100...10000).contains(timeoutMs ?? 5000),(8...64).contains(memoryMiB ?? 32),(1...100).contains(maxCommands ?? 100)
        else{throw AppError.message("Interpreter budget outside supported bounds")}
        return self
    }
}
struct HotConfigurationStore:Codable,Equatable{
    var active:HotConfiguration?=nil
    var previous:HotConfiguration?=nil
    var revision:String{active?.revision ?? "native-baseline"}
    func checked()throws->Self{
        _ = try active?.checked();_ = try previous?.checked()
        return self
    }
    func activating(_ candidate:HotConfiguration?,expected:String)throws->Self{
        guard expected==revision else{throw AppError.message("Configuration revision changed; refresh before applying")}
        if let candidate=candidate{
            _ = try candidate.checked()
            guard candidate.revision != "native-baseline",candidate.revision != revision else{throw AppError.message("Use a new configuration revision")}
        }
        return HotConfigurationStore(active:candidate,previous:active)
    }
}

// A staged file is not active until its exact revision and base are requested.
struct StagedHotConfiguration:Codable,Equatable{
    var expectedRevision:String
    var candidate:HotConfiguration
    func checked(against current:HotConfigurationStore)throws->Self{
        _ = try current.activating(candidate,expected:expectedRevision)
        return self
    }
}
struct HotConfigurationTrial:Codable,Equatable{
    var id=UUID().uuidString
    var before:HotConfigurationStore
    var candidateRevision:String
    var startedAt:Date=Date()
    var expiresAt:Date
    var pending=true
    var outcome:String?=nil
    func checked()throws->Self{
        _ = try before.checked()
        guard candidateRevision != before.revision,DynamicUILayout.validID(candidateRevision),
              DynamicUILayout.validID(id),expiresAt.timeIntervalSince(startedAt)>0,
              expiresAt.timeIntervalSince(startedAt)<=3601 else{throw AppError.message("Invalid configuration trial journal")}
        return self
    }
    func expired(at now:Date)->Bool{pending && now>=expiresAt}
}
