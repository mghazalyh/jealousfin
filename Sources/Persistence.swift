import Foundation

// Durable retention accounting. This file is outside the event- namespace so
// pruning can never delete its own evidence.
struct DiagnosticRetentionState: Codable, Equatable {
    var schemaVersion=1
    var lastAttempt:Date?
    var lastSuccess:Date?
    var lastError:String?
    var totalEventsRemoved=0
    var totalBytesRemoved=0
    var lastEventCount:Int?
    var lastEventBytes:Int?
    var lastTrimMs:Int?
}
// A launch marker is written before serving startup work and marked clean only
// by an explicit shutdown call. A marker left unclean is suspected crash or
// watchdog evidence, never proof that a particular subsystem failed.
struct DiagnosticLaunchEvidence: Codable, Equatable {
    var schemaVersion=1
    var launchID:String
    var startedAt:Date
    var previousLaunchID:String?
    var cleanShutdown=false
    var eventCount:Int?
    var eventBytes:Int?
}
struct DiagnosticPruneResult: Equatable {
    var removed:Int
    var bytes:Int
    var removedNames:[String]
    var failed:Bool
}

@MainActor final class Disk {
    private static let retentionName="diagnostic-retention-v1.json"
    private static let launchName="diagnostic-launch-v1.json"
    private static let maxLoadBytes=16*1024*1024
    // Safety ceiling independent of hot configuration. Hot retention may ask
    // for less; these values only stop unbounded diagnostic growth if the
    // application never reaches its scheduled retention pass.
    private static let hardMaxEvents=5000
    private static let hardMaxBytes=32*1024*1024
    let root: URL
    private(set) var error: String?
    private(set) var retention:DiagnosticRetentionState
    private(set) var previousUncleanLaunch:DiagnosticLaunchEvidence?
    private(set) var diagnosticLaunchID=UUID().uuidString
    private var eventIndex:[String:Int]?
    private var batchIndex:[String:Int]?
    private var trimInFlight=false
    init(root override:URL? = nil) {
        let storage=override ?? FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask).first!.appendingPathComponent("Jealousfin",isDirectory:true)
        root=storage
        do { try FileManager.default.createDirectory(at:storage,withIntermediateDirectories:true) } catch { self.error="Cannot create app storage" }
        let previous:DiagnosticLaunchEvidence?=Self.readEvidence(root:storage,name:Self.launchName)
        retention=Self.readEvidence(root:storage,name:Self.retentionName) ?? DiagnosticRetentionState()
        if let previous=previous,!previous.cleanShutdown {
            previousUncleanLaunch=previous
            Self.writeCrashEvidence(root:storage,previous:previous,retention:retention)
        }
        Self.writeLaunchEvidence(root:storage,launchID:diagnosticLaunchID,previous:previous)
    }
    private static func readEvidence<T:Decodable>(root:URL,name:String)->T?{
        let url=root.appendingPathComponent(name)
        guard let data=try? Data(contentsOf:url) else{return nil}
        return try? JSONCodec.decoder().decode(T.self,from:data)
    }
    private static func writeEvidence<T:Encodable>(root:URL,name:String,_ value:T){
        guard let data=try? JSONCodec.encoder().encode(value) else{return}
        try? data.write(to:root.appendingPathComponent(name),options:.atomic)
    }
    private static func writeCrashEvidence(root:URL,previous:DiagnosticLaunchEvidence,retention:DiagnosticRetentionState){
        let event=Event(severity:"WARN",component:"lifecycle",code:"UNCLEAN_LAUNCH",fields:[
            "previousLaunchID":previous.launchID,
            "previousStartedAt":ISO8601DateFormatter().string(from:previous.startedAt),
            "previousCleanShutdown":String(previous.cleanShutdown),
            "previousEventCount":previous.eventCount.map{String($0)} ?? "",
            "lastKnownEventCount":retention.lastEventCount.map{String($0)} ?? "",
            "lastKnownEventBytes":retention.lastEventBytes.map{String($0)} ?? "",
            "confidence":"suspected"
        ])
        writeEvidence(root:root,name:"event-\(event.id).json",event)
    }
    private static func writeLaunchEvidence(root:URL,launchID:String,previous:DiagnosticLaunchEvidence?){
        let evidence=DiagnosticLaunchEvidence(launchID:launchID,startedAt:Date(),previousLaunchID:previous?.launchID,cleanShutdown:false,eventCount:nil,eventBytes:nil)
        writeEvidence(root:root,name:launchName,evidence)
    }
    // Called from the application shutdown/background-complete path. Absence of
    // this call is intentionally reported as an unclean prior launch.
    func markCleanShutdown(){
        guard var evidence:DiagnosticLaunchEvidence=Self.readEvidence(root:root,name:Self.launchName),
              evidence.launchID==diagnosticLaunchID else{return}
        evidence.cleanShutdown=true
        evidence.eventCount=eventIndex?.count ?? retention.lastEventCount
        evidence.eventBytes=eventIndex?.values.reduce(0,+) ?? retention.lastEventBytes
        Self.writeEvidence(root:root,name:Self.launchName,evidence)
    }
    func load<T: Decodable>(_ type:T.Type,_ name:String) -> T? {
        let url=root.appendingPathComponent(name)
        guard let attributes=try? FileManager.default.attributesOfItem(atPath:url.path),
              let size=attributes[.size] as? NSNumber,size.intValue<=Self.maxLoadBytes else { return nil }
        do { return try JSONCodec.decoder().decode(type,from:Data(contentsOf:url)) }
        catch { self.error="Cannot read \(name)"; return nil }
    }
    @discardableResult func save<T:Encodable>(_ object:T,_ name:String) -> Bool {
        let url=root.appendingPathComponent(name)
        let isEvent=name.hasPrefix("event-"),isBatch=name.hasPrefix("batch-")
        if isEvent,eventIndex==nil{refreshEventIndex()}
        if isBatch,batchIndex==nil{refreshBatchIndex()}
        let data:Data
        do { data=try JSONCodec.encoder().encode(object) }
        catch { self.error="Cannot write \(name)"; return false }
        do {
            try data.write(to:url,options:.atomic)
            if isEvent,eventIndex != nil {eventIndex?[name]=data.count}
            if isBatch,batchIndex != nil {batchIndex?[name]=data.count}
            if isEvent {scheduleEmergencyTrimIfNeeded()}
            return true
        }
        catch { self.error="Cannot write \(name)"; return false }
    }
    // Diagnostic appends are immutable, uniquely named events. Encoding and
    // the atomic write run off the MainActor; only exact index accounting is
    // applied back on the actor. If the index was never derived it stays nil so
    // the next count performs one exact scan rather than trusting a stale value.
    func appendEventAsync(_ event:Event,_ name:String) async -> Bool {
        guard name.hasPrefix("event-") else{return save(event,name)}
        let directory=root
        guard let data=try? JSONCodec.encoder().encode(event) else{error="Cannot write \(name)";return false}
        let wrote=await Task.detached(priority:.utility){ ()->Bool in
            do{try data.write(to:directory.appendingPathComponent(name),options:.atomic);return true}
            catch{return false}
        }.value
        guard wrote else{error="Cannot write \(name)";return false}
        if eventIndex != nil{eventIndex?[name]=data.count}
        scheduleEmergencyTrimIfNeeded()
        return true
    }
    private nonisolated static func scanIndex(root:URL,prefix:String)->[String:Int]? {
        let keys:[URLResourceKey]=[.fileSizeKey,.isRegularFileKey]
        guard let urls=try? FileManager().contentsOfDirectory(at:root,includingPropertiesForKeys:keys) else{return nil}
        var index:[String:Int]=[:]
        for url in urls {
            let name=url.lastPathComponent
            guard name.hasPrefix(prefix),url.pathExtension=="json",
                  let v=try? url.resourceValues(forKeys:Set(keys)),v.isRegularFile==true else{continue}
            index[name]=v.fileSize ?? 0
        }
        return index
    }
    private nonisolated static func scanNames(root:URL,prefix:String)->[String]? {
        let keys:[URLResourceKey]=[.isRegularFileKey]
        guard let urls=try? FileManager().contentsOfDirectory(at:root,includingPropertiesForKeys:keys) else{return nil}
        var names:[String]=[]
        for url in urls {
            let name=url.lastPathComponent
            guard name.hasPrefix(prefix),url.pathExtension=="json",
                  let v=try? url.resourceValues(forKeys:Set(keys)),v.isRegularFile==true else{continue}
            names.append(name)
        }
        return names.sorted()
    }
    private func refreshEventIndex(){eventIndex=Self.scanIndex(root:root,prefix:"event-")}
    private func refreshBatchIndex(){batchIndex=Self.scanIndex(root:root,prefix:"batch-")}
    func files(prefix:String) -> [URL] {
        if prefix=="event-" {
            if eventIndex==nil{refreshEventIndex()}
            if let index=eventIndex{return index.keys.sorted().map{root.appendingPathComponent($0)}}
            return []
        }
        if prefix=="batch-" {
            if batchIndex==nil{refreshBatchIndex()}
            if let index=batchIndex{return index.keys.sorted().map{root.appendingPathComponent($0)}}
            return []
        }
        return (Self.scanNames(root:root,prefix:prefix) ?? []).map{root.appendingPathComponent($0)}
    }
    func queuedCount(prefix:String)->Int {
        if prefix=="event-" {
            if eventIndex==nil{refreshEventIndex()}
            return eventIndex?.count ?? 0
        }
        if prefix=="batch-" {
            if batchIndex==nil{refreshBatchIndex()}
            return batchIndex?.count ?? 0
        }
        return Self.scanNames(root:root,prefix:prefix)?.count ?? 0
    }
    // Only immutable diagnostic events are pruned. Receipts, health, patches,
    // settings, rotation, programme/language data and queued batches are
    // deliberately excluded. The worker never accesses actor-isolated state.
    // Every event-*.json entry is considered so an unreadable entry cannot hide
    // the count bound, but non-regular entries are reported rather than removed.
    private nonisolated static func pruneEvents(root:URL,maxCount:Int,maxBytes:Int)->DiagnosticPruneResult{
        let fm=FileManager()
        let keys:[URLResourceKey]=[.fileSizeKey,.creationDateKey,.isRegularFileKey]
        guard let files=try? fm.contentsOfDirectory(at:root,includingPropertiesForKeys:keys) else{
            return DiagnosticPruneResult(removed:0,bytes:0,removedNames:[],failed:true)
        }
        var candidates:[(URL,Int,Date)]=[]
        var failed=false
        for url in files {
            guard url.lastPathComponent.hasPrefix("event-"),url.pathExtension=="json" else{continue}
            let v=try? url.resourceValues(forKeys:Set(keys))
            guard v?.isRegularFile==true else{failed=true;continue}
            candidates.append((url,v?.fileSize ?? 0,v?.creationDate ?? .distantPast))
        }
        candidates.sort{a,b in a.2==b.2 ? a.0.lastPathComponent<b.0.lastPathComponent:a.2<b.2}
        var count=candidates.count,total=candidates.reduce(0){$0+$1.1},removed=0,bytes=0
        var removedNames:[String]=[]
        for entry in candidates{
            guard count>maxCount || total>maxBytes else{break}
            do{try fm.removeItem(at:entry.0);count-=1;total-=entry.1;removed+=1;bytes+=entry.1;removedNames.append(entry.0.lastPathComponent)}
            catch{failed=true}
        }
        return DiagnosticPruneResult(removed:removed,bytes:bytes,removedNames:removedNames,failed:failed)
    }
    private func apply(_ result:DiagnosticPruneResult){
        if result.failed{eventIndex=nil}
        else if eventIndex != nil{for name in result.removedNames{eventIndex?.removeValue(forKey:name)}}
    }
    private func record(_ result:DiagnosticPruneResult,startedAt:TimeInterval){
        retention.lastAttempt=Date()
        if result.failed{retention.lastError="Diagnostic retention cleanup failed";error=retention.lastError}
        else{retention.lastSuccess=Date();retention.lastError=nil}
        retention.totalEventsRemoved+=result.removed
        retention.totalBytesRemoved+=result.bytes
        retention.lastEventCount=eventIndex?.count
        retention.lastEventBytes=eventIndex?.values.reduce(0,+)
        retention.lastTrimMs=Int((ProcessInfo.processInfo.systemUptime-startedAt)*1000)
        Self.writeEvidence(root:root,name:Self.retentionName,retention)
    }
    func trimDiagnosticEvents(maxCount:Int,maxBytes:Int)->(removed:Int,bytes:Int){
        let started=ProcessInfo.processInfo.systemUptime
        let result=Self.pruneEvents(root:root,maxCount:maxCount,maxBytes:maxBytes)
        apply(result);record(result,startedAt:started)
        return(result.removed,result.bytes)
    }
    func trimDiagnosticEventsAsync(maxCount:Int,maxBytes:Int)async->(removed:Int,bytes:Int){
        guard !trimInFlight else{return(0,0)}
        trimInFlight=true
        defer{trimInFlight=false}
        let started=ProcessInfo.processInfo.systemUptime
        let directory=root
        let result=await Task.detached(priority:.utility){
            Self.pruneEvents(root:directory,maxCount:maxCount,maxBytes:maxBytes)
        }.value
        apply(result);record(result,startedAt:started)
        return(result.removed,result.bytes)
    }
    private func scheduleEmergencyTrimIfNeeded(){
        guard !trimInFlight,let index=eventIndex else{return}
        let bytes=index.values.reduce(0,+)
        guard index.count>Self.hardMaxEvents || bytes>Self.hardMaxBytes else{return}
        trimInFlight=true
        let maxCount=Self.hardMaxEvents,maxBytes=Self.hardMaxBytes
        Task{[weak self] in
            guard let self=self else{return}
            let started=ProcessInfo.processInfo.systemUptime
            let directory=self.root
            let result=await Task.detached(priority:.utility){
                Self.pruneEvents(root:directory,maxCount:maxCount,maxBytes:maxBytes)
            }.value
            self.apply(result);self.record(result,startedAt:started)
            self.trimInFlight=false
        }
    }
    func quarantineUnreadableConfigurationTrial()throws{
        let path=root.appendingPathComponent("hot-config-trial.json")
        guard FileManager.default.fileExists(atPath:path.path) else{return}
        if let trial=load(HotConfigurationTrial.self,"hot-config-trial.json"),(try? trial.checked()) != nil{return}
        let quarantine=root.appendingPathComponent("hot-config-recovery-"+UUID().uuidString+".json")
        try FileManager.default.moveItem(at:path,to:quarantine)
    }
    func recoverConfigurationTrial()throws->HotConfigurationTrial?{
        let path=root.appendingPathComponent("hot-config-trial.json")
        guard FileManager.default.fileExists(atPath:path.path) else{return nil}
        guard var trial=load(HotConfigurationTrial.self,"hot-config-trial.json") else{throw AppError.message("Cannot decode configuration trial journal")}
        _ = try trial.checked()
        guard trial.pending else{return trial}
        try saveConfiguration(trial.before)
        trial.pending=false;trial.outcome="recovered-on-launch"
        guard save(trial,"hot-config-trial.json") else{throw AppError.message("Cannot finalize configuration recovery")}
        return trial
    }
    func saveConfiguration(_ candidate:HotConfigurationStore)throws{
        _ = try candidate.checked()
        if let config=candidate.active {
            let file="hot-config-version-"+config.revision+".json"
            if FileManager.default.fileExists(atPath:root.appendingPathComponent(file).path){
                guard load(HotConfiguration.self,file)==config else{throw AppError.message("Immutable configuration revision collision")}
            }else{
                guard save(config,file) else{throw AppError.message("Cannot archive configuration")}
            }
        }
        guard save(candidate,"hot-config.json") else{throw AppError.message("Cannot activate configuration")}
    }
    func remove(_ url:URL) {
        let name=url.lastPathComponent
        do {
            try FileManager.default.removeItem(at:url)
            if name.hasPrefix("event-"),eventIndex != nil{eventIndex?.removeValue(forKey:name)}
            if name.hasPrefix("batch-"),batchIndex != nil{batchIndex?.removeValue(forKey:name)}
        } catch { self.error="Cannot remove uploaded batch" }
    }
    func loadRecentEvents(limit:Int) async -> [Event] {
        guard limit>0 else{return []}
        let directory=root
        return await Task.detached(priority:.utility){
            Self.readRecentEvents(root:directory,limit:limit)
        }.value
    }
    private nonisolated static func readRecentEvents(root:URL,limit:Int)->[Event]{
        let keys:[URLResourceKey]=[.creationDateKey,.isRegularFileKey]
        guard let urls=try? FileManager().contentsOfDirectory(at:root,includingPropertiesForKeys:keys) else{return []}
        var entries:[(URL,Date,String)]=[]
        for url in urls {
            let name=url.lastPathComponent
            guard name.hasPrefix("event-"),url.pathExtension=="json",
                  let v=try? url.resourceValues(forKeys:Set(keys)),v.isRegularFile==true else{continue}
            entries.append((url,v.creationDate ?? .distantPast,name))
        }
        entries.sort{$0.1 == $1.1 ? $0.2 < $1.2 : $0.1 < $1.1}
        var events:[Event]=[]
        for entry in entries.suffix(limit) {
            guard let data=try? Data(contentsOf:entry.0),
                  let event=try? JSONCodec.decoder().decode(Event.self,from:data) else{continue}
            events.append(event)
        }
        return events
    }
}

@MainActor final class EventAppendQueue {
    private let disk:Disk
    private let maxPending:Int
    private var pending:[(Event,String)]=[]
    private var drainTask:Task<Void,Never>?
    private(set) var dropped=0
    init(disk:Disk,maxPending:Int=512){
        self.disk=disk
        self.maxPending=max(1,maxPending)
    }
    var pendingCount:Int{pending.count}
    func enqueue(_ event:Event,name:String){
        guard name.hasPrefix("event-") else{return}
        if pending.count>=maxPending{pending.removeFirst();dropped+=1}
        pending.append((event,name))
        if drainTask==nil{
            drainTask=Task{[weak self] in await self?.drain()}
        }
    }
    func drain() async {
        while !pending.isEmpty {
            let item=pending.removeFirst()
            _=await disk.appendEventAsync(item.0,item.1)
        }
        drainTask=nil
    }
    func flush() async {
        while let task=drainTask {
            await task.value
        }
        if !pending.isEmpty {
            drainTask=Task{[weak self] in await self?.drain()}
            await drainTask?.value
        }
    }
}
struct GitFile {
    var sha: String
    var data: Data
}
private struct TorrioWriteFailure:LocalizedError {
    let status:Int
    var errorDescription:String?{"JealousDB write HTTP \(status)"}
}
private final class DatabaseNoRedirect:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
}
@MainActor final class TorrioDB {
    private let transport:URLSession
    let repositoryAddress:String?
    private let repository:String?
    init(address:String,transport:URLSession = .shared){
        self.transport=transport
        self.repositoryAddress=GitHubRepositoryAddress.canonical(address)
        self.repository=GitHubRepositoryAddress.apiRoot(address)
    }
    private let writeAttempts=4
    private func conflictBackoff(_ attempt:Int) async throws {
        try await Task.sleep(nanoseconds:UInt64(250_000_000 * (1 << attempt)))
    }
    var base:String? {repository.map{$0+"contents/jealousfin/"}}
    var token = ""
    var enabled=true
    var configured:Bool { enabled && repository != nil && !token.isEmpty }
    func call(_ path:String, method:String="GET", body:[String:Any]?=nil,gitTree:Bool=false) async throws -> (Data,Int) {
        guard enabled else{throw AppError.message("LAN control selected: GitHub disabled")}
        try Task.checkCancellation()
        guard let repository=repository,let base=base else{throw AppError.message("Invalid GitHub repository address")}
        guard !token.isEmpty else{throw AppError.message("Configure GitHub repository token")}
        guard var components=URLComponents(string:(gitTree ? repository+"git/trees/" : base)+path),
              components.scheme=="https",components.host=="api.github.com",components.fragment==nil,
              components.query==nil else{throw AppError.message("Invalid database request path")}
        if method=="GET" && !gitTree{components.queryItems=[URLQueryItem(name:"ref",value:"main")]}
        guard let u=components.url else{throw AppError.message("Invalid database request URL")}
        var req=URLRequest(url:u); req.httpMethod=method; req.timeoutInterval=20
        req.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")
        req.setValue("application/vnd.github+json",forHTTPHeaderField:"Accept")
        req.setValue("2022-11-28",forHTTPHeaderField:"X-GitHub-Api-Version")
        if let body=body { req.httpBody=try JSONSerialization.data(withJSONObject:body); req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (d,r)=try await transport.data(for:req,delegate:DatabaseNoRedirect())
        guard let h=r as? HTTPURLResponse else { throw AppError.message("Invalid GitHub response") }
        return (d,h.statusCode)
    }
    func privateConnectionDefaults()async throws->JellyfinConnectionDefaults {
        guard enabled,repositoryAddress==GitHubRepositoryAddress.defaultURL,
              let repository=repository,let endpoint=URL(string:String(repository.dropLast())),!token.isEmpty
        else{throw AppError.message("Private JealousDB repository and token required")}
        var request=URLRequest(url:endpoint);request.timeoutInterval=15
        request.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")
        request.setValue("application/vnd.github+json",forHTTPHeaderField:"Accept")
        let (data,response)=try await transport.data(for:request,delegate:DatabaseNoRedirect())
        guard (response as? HTTPURLResponse)?.statusCode==200,
              let metadata=try JSONSerialization.jsonObject(with:data) as? [String:Any],
              metadata["private"] as? Bool==true,
              metadata["full_name"] as? String=="mghazalyh/jealousdb"
        else{throw AppError.message("Connection defaults require the verified private JealousDB")}
        guard let file=try await read("database/connection-defaults-v1.json"),file.data.count<=16384
        else{throw AppError.message("Private connection defaults are not configured")}
        return try JSONCodec.decoder().decode(PrivateConnectionDefaults.self,from:file.data).checked()
    }
    func read(_ path:String) async throws -> GitFile? {
        let (d,status)=try await call(path)
        if status==404 { return nil }
        guard status==200, let j=try JSONSerialization.jsonObject(with:d) as? [String:Any], let sha=j["sha"] as? String, let c=j["content"] as? String, let bytes=Data(base64Encoded:c,options:.ignoreUnknownCharacters) else { throw AppError.message("JealousDB read HTTP \(status)") }
        return GitFile(sha:sha,data:bytes)
    }
    private func writeOnce(_ path:String,data:Data,sha:String?) async throws {
        var b:[String:Any]=["message":"jealousfin: synchronize \(path)","content":data.base64EncodedString(),"branch":"main"]
        if let sha=sha {b["sha"]=sha}
        let (_,status)=try await call(path,method:"PUT",body:b)
        guard status==200 || status==201 else{throw TorrioWriteFailure(status:status)}
    }
    // Preserve compare-and-swap semantics for reviewed baseline promotion.
    // A branch-head conflict can be retried only while the target blob is unchanged.
    func put(_ path:String,data:Data,sha:String?=nil) async throws {
        for attempt in 0..<writeAttempts {
            do{try await writeOnce(path,data:data,sha:sha);return}
            catch let error as TorrioWriteFailure where error.status==409 {
                let current=try await read(path)
                if current?.data==data{return}
                guard current?.sha==sha,attempt+1<writeAttempts else{throw error}
                try await conflictBackoff(attempt)
            }
        }
    }
    // For device-owned mutable observations only, re-read the current blob
    // and replace it. Never use this operation for immutable data or promotion.
    func replaceDeviceSnapshot(_ path:String,data:Data) async throws {
        for attempt in 0..<writeAttempts {
            let current=try await read(path)
            if current?.data==data{return}
            do{try await writeOnce(path,data:data,sha:current?.sha);return}
            catch let error as TorrioWriteFailure where error.status==409 && attempt+1<writeAttempts {
                try await conflictBackoff(attempt)
            }
        }
    }
    func immutable(_ path:String,data:Data) async throws {
        for attempt in 0..<writeAttempts {
            if let current=try await read(path){
                guard current.data==data else{throw AppError.message("Immutable record collision")}
                return
            }
            do{try await writeOnce(path,data:data,sha:nil);return}
            catch let error as TorrioWriteFailure where error.status==409 {
                if let current=try await read(path){
                    guard current.data==data else{throw AppError.message("Immutable record collision")}
                    return
                }
                guard attempt+1<writeAttempts else{throw error}
                try await conflictBackoff(attempt)
            }
        }
    }
    func directory(_ path:String) async throws -> [String] {
        let (d,status)=try await call(path)
        if status==404 { return [] }
        guard status==200, let a=try JSONSerialization.jsonObject(with:d) as? [[String:Any]] else { throw AppError.message("JealousDB listing HTTP \(status)") }
        var names=a.filter{$0["type"] as? String=="file"}.compactMap{$0["name"] as? String}
        // Contents API caps directory listings at 1000 entries. Resolve its tree
        // instead of silently losing later commands/receipts after long operation.
        if a.count>=1000 {
            let parent=(path as NSString).deletingLastPathComponent
            let name=(path as NSString).lastPathComponent
            let (pd,ps)=try await call(parent)
            guard ps==200,let children=try JSONSerialization.jsonObject(with:pd) as? [[String:Any]],
                  let sha=children.first(where:{$0["name"] as? String==name && $0["type"] as? String=="dir"})?["sha"] as? String,
                  sha.range(of:"^[0-9a-f]{40}$",options:.regularExpression) != nil else{throw AppError.message("Cannot resolve complete command directory")}
            let (td,ts)=try await call(sha,gitTree:true)
            guard ts==200,let tree=try JSONSerialization.jsonObject(with:td) as? [String:Any],tree["truncated"] as? Bool==false,
                  let entries=tree["tree"] as? [[String:Any]] else{throw AppError.message("Incomplete Git tree")}
            names=entries.filter{$0["type"] as? String=="blob"}.compactMap{$0["path"] as? String}
        }
        return names.filter{$0.hasSuffix(".json") && $0.range(of:"^[A-Za-z0-9_.-]+$",options:.regularExpression) != nil}
    }
    func merge(_ local:HealthLedger) async throws -> HealthLedger {
        let path="database/channel-health-v1.json"
        for attempt in 0..<writeAttempts {
            let file=try await read(path)
            var merged=local
            if let file=file{merged.merge(try JSONCodec.decoder().decode(HealthLedger.self,from:file.data))}
            merged.events.sort{$0.at == $1.at ? $0.id < $1.id : $0.at < $1.at}
            let bytes=try JSONCodec.encoder().encode(merged)
            if file?.data==bytes{return merged}
            do{try await writeOnce(path,data:bytes,sha:file?.sha);return merged}
            catch let error as TorrioWriteFailure where error.status==409 && attempt+1<writeAttempts {
                try await conflictBackoff(attempt)
            }
        }
        throw TorrioWriteFailure(status:409)
    }
}
