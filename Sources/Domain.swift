import Foundation

struct Program: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var start: Date?
    var end: Date?
    var channelID: String?
    var title: String {
        let p = name.components(separatedBy: " · ")
        return p.count > 1 ? p.dropFirst().joined(separator: " · ") : name
    }
    var progress: Double? {
        guard let a = start, let b = end, b > a else { return nil }
        return min(1, max(0, Date().timeIntervalSince(a) / b.timeIntervalSince(a)))
    }
}
struct Channel: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var number: String?
    var logo: Bool
    var favorite: Bool
    var program: Program?
    var genres: [String]
    var isSurf: Bool { name.hasPrefix("Surf:") }
}
enum SharedSurf {
    // The LAN gateway is a separate authenticated service on the same configured
    // server. Only the explicitly supported private IPv4 Jellyfin port pairing
    // can use it; no arbitrary cross-host or public HTTP token forwarding.
    static func gatewayOrigin(for server:URL,configuration:GatewayConfiguration=GatewayConfiguration())->URL? {
        guard (try? configuration.checked()) != nil,var c=URLComponents(url:server,resolvingAgainstBaseURL:false),
              c.user==nil,c.password==nil,c.query==nil,c.fragment==nil,
              c.path.isEmpty || c.path=="/",let host=c.host else{return nil}
        if c.scheme?.lowercased()=="https" {c.path="";return c.url}
        guard c.scheme?.lowercased()=="http",c.port==(configuration.lanServerPort ?? 8096) else{return nil}
        let octets=host.split(separator:".").compactMap{UInt8($0)}
        guard octets.count==4,host==octets.map{String($0)}.joined(separator:"."),
              octets[0]==10 || (octets[0]==172 && (16...31).contains(octets[1])) || (octets[0]==192 && octets[1]==168)
        else{return nil}
        c.port=configuration.lanGatewayPort ?? 8097;c.path="";return c.url
    }
    static func mode(for category:String)->String? {
        let value=category.trimmingCharacters(in:.whitespacesAndNewlines).lowercased()
        switch value {
        case "all channels","all":return "all"
        case "movies & series","movies-series":return "movies-series"
        case "sports":return "sports"
        default:return nil
        }
    }
    static func mode(for channel:Channel)->String? {
        guard channel.isSurf else{return nil}
        return mode(for:String(channel.name.dropFirst("Surf:".count)))
    }
    static func channel(for category:String,in channels:[Channel])->Channel? {
        guard let mode=mode(for:category) else{return nil}
        return channels.first{Self.mode(for:$0)==mode}
    }
    static func ticketURL(_ raw:String,origin:URL,mode:String,configuration:GatewayConfiguration=GatewayConfiguration())throws->URL {
        _ = try configuration.checked()
        let expected=configuration.path(mode:mode,leaf:"index.m3u8")
        guard ["all","movies-series","sports"].contains(mode),
              let parts=URLComponents(string:raw),parts.scheme==nil,parts.host==nil,
              parts.user==nil,parts.password==nil,parts.fragment==nil,
              parts.percentEncodedPath==expected,
              let items=parts.queryItems,items.count==1,items[0].name=="ticket",
              let ticket=items[0].value,!ticket.isEmpty,ticket.utf8.count<=8192,
              let url=URL(string:raw,relativeTo:origin)?.absoluteURL,
              JellyfinAdapterRules.sameOrigin(url,origin)
        else{throw AppError.message("Invalid shared Surf ticket URL")}
        return url
    }
}
struct HealthPenaltyPolicy:Codable,Equatable{
    var baseDays:Int=3
    var banAfter:Int=8
    func checked()throws->Self{
        guard (1...30).contains(baseDays),(2...20).contains(banAfter) else{throw AppError.message("Invalid progressive hiatus policy")}
        return self
    }
}
struct ChannelHealth: Codable, Equatable {
    var failures = 0
    var lifetimeFailures = 0
    var successes = 0
    var lastFailure: Date?
    var lastSuccess: Date?
    var hiatusUntil: Date?
    var banned = false
    var reason: String?
    var approvedAt: Date?
    func eligible(at date: Date) -> Bool { !banned && (hiatusUntil.map { $0 <= date } ?? true) }
    mutating func fail(at date: Date, reason: String, policy:HealthPenaltyPolicy=HealthPenaltyPolicy()) {
        guard eligible(at: date) else { return }
        failures += 1
        lifetimeFailures += 1
        lastFailure = date
        self.reason = reason
        if failures >= policy.banAfter { banned = true; hiatusUntil = nil }
        else { hiatusUntil = date.addingTimeInterval(Double(failures * policy.baseDays) * 86400) }
    }
    mutating func succeed(at date: Date) {
        guard eligible(at: date) else { return }
        failures = 0; successes += 1; lastSuccess = date; hiatusUntil = nil
    }
    mutating func approve(at date: Date) {
        guard banned else { return }
        banned = false; failures = 0; hiatusUntil = nil; approvedAt = date
    }
}
struct HealthEvent: Codable, Identifiable, Equatable {
    var id = UUID().uuidString
    var at = Date()
    var channelID: String
    var channelName: String
    var action: String
    var reason: String
    var penalty:HealthPenaltyPolicy?=nil
}
struct HealthLedger: Codable {
    var schemaVersion = 1
    var events: [HealthEvent] = []
    func states() -> [String: ChannelHealth] {
        var result: [String: ChannelHealth] = [:]
        var seen = Set<String>()
        for e in events.sorted(by: { $0.at == $1.at ? $0.id < $1.id : $0.at < $1.at }) {
            guard seen.insert(e.id).inserted else { continue }
            var s = result[e.channelID] ?? ChannelHealth()
            switch e.action {
            case "failure": s.fail(at: e.at, reason: e.reason,policy:(try? e.penalty?.checked()) ?? HealthPenaltyPolicy())
            case "success": s.succeed(at: e.at)
            case "approve": s.approve(at: e.at)
            default: continue
            }
            result[e.channelID] = s
        }
        return result
    }
    mutating func merge(_ other: HealthLedger) {
        var ids = Set(events.map(\.id))
        for e in other.events where ids.insert(e.id).inserted { events.append(e) }
        events.sort { $0.at == $1.at ? $0.id < $1.id : $0.at < $1.at }
    }
}
struct Event: Codable, Identifiable {
    var schemaVersion = 1
    var id = UUID().uuidString
    var timestamp = Date()
    var severity: String
    var component: String
    var code: String
    var trace: String?
    var fields: [String: String] = [:]
}
struct Insight: Identifiable, Codable, Equatable {
    var id: String
    var priority: String
    var title: String
    var explanation: String
    var action: String
}
struct Command: Codable, Identifiable {
    var schemaVersion = 1
    var id: String
    var action: String
    var arguments: [String: String]?
    var expiresAt: Date?
}
struct Receipt: Codable {
    var schemaVersion = 1
    var id: String
    var action: String
    var status: String
    var result: String
    var timestamp = Date()
}
// Only canonical GitHub repository addresses are accepted. Derive the API
// origin from validated owner/name components, never from an arbitrary URL.
enum GitHubRepositoryAddress {
    static let defaultURL="https://github.com/mghazalyh/jealousdb"
    static func canonical(_ input:String)->String? {
        let raw=input.trimmingCharacters(in:.whitespacesAndNewlines)
        guard let c=URLComponents(string:raw),c.scheme?.lowercased()=="https",
              c.host?.lowercased()=="github.com",c.port==nil,c.user==nil,c.password==nil,
              c.query==nil,c.fragment==nil,c.percentEncodedPath==c.path else{return nil}
        let path=c.path.hasSuffix("/") ? String(c.path.dropLast()) : c.path
        guard path.range(of:"^/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$",options:.regularExpression) != nil else{return nil}
        let parts=path.split(separator:"/")
        guard parts.count==2,parts[1] != ".",parts[1] != "..",!parts[1].hasSuffix(".git") else{return nil}
        return "https://github.com"+path.lowercased()
    }
    static func apiRoot(_ input:String)->String? {
        guard let address=canonical(input),let path=URLComponents(string:address)?.path else{return nil}
        return "https://api.github.com/repos"+path+"/"
    }
}
// Private deployment defaults are optional bundle configuration, not credentials
// committed to the public source repository or public release artifacts.
struct JellyfinConnectionDefaults {
    var server:String
    var username:String
    var password:String
    static func bundled(_ bundle:Bundle = .main)->JellyfinConnectionDefaults? {
        guard let url=bundle.url(forResource:"JealousfinDefaults",withExtension:"plist"),
              let data=try? Data(contentsOf:url),
              let values=(try? PropertyListSerialization.propertyList(from:data,options:[],format:nil)) as? [String:String],
              let server=values["server"],let username=values["username"],let password=values["password"],
              let parsed=URLComponents(string:server),["http","https"].contains(parsed.scheme ?? ""),
              parsed.host != nil,parsed.user==nil,parsed.password==nil,parsed.query==nil,parsed.fragment==nil
        else{return nil}
        return JellyfinConnectionDefaults(server:server,username:username,password:password)
    }
}
struct PrivateConnectionDefaults:Codable {
    var schemaVersion:Int
    var server:String
    var username:String
    var password:String
    func checked()throws->JellyfinConnectionDefaults {
        guard schemaVersion==1,let u=URLComponents(string:server),
              ["http","https"].contains(u.scheme ?? ""),u.host != nil,u.user==nil,u.password==nil,u.query==nil,u.fragment==nil,
              !username.isEmpty,username.utf8.count<=256,!password.isEmpty,password.utf8.count<=1024,server.utf8.count<=2048
        else{throw AppError.message("Invalid private connection defaults")}
        return JellyfinConnectionDefaults(server:server,username:username,password:password)
    }
}
struct Settings: Codable {
    var server = ""
    var username = ""
    var password = ""
    var githubToken = ""
    var provisionFromPrivateDB:Bool? = nil
    // Optional on disk so decoding older settings preserves credentials,
    // player preferences and all other fields during an upgrade.
    var githubRepositoryURL:String?
    var repositoryAddress:String {
        get {githubRepositoryURL ?? GitHubRepositoryAddress.defaultURL}
        set {githubRepositoryURL=newValue}
    }
    var rotationMinutes = 5
    var videoBrightness:Int?=nil
    var retries = 2
    var startupTimeout = 15
    var validation = true
    var random = false
    var unattended = true
    var lanEnabled = true
    var remoteControlMode:RemoteControlMode?
    var lanAccessToken:String?
    var controlMode:RemoteControlMode {
        get {remoteControlMode ?? .lan}
        set {remoteControlMode=newValue}
    }
    var qualityMbps = 10
    var autoLogin = true
    var resumeSurf = false
    var surfCategory = "All Channels"
    // Fill missing fields only; never overwrite an already configured server or
    // mix credentials from a different server into a saved account.
    mutating func applyConnectionDefaults(_ defaults:JellyfinConnectionDefaults?) {
        guard let defaults=defaults else{return}
        if server.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty {
            server=defaults.server
        }
        guard server.trimmingCharacters(in:.whitespacesAndNewlines).trimmingCharacters(in:CharacterSet(charactersIn:"/")) == defaults.server.trimmingCharacters(in:CharacterSet(charactersIn:"/")) else{return}
        if username.isEmpty{username=defaults.username}
        if username==defaults.username && password.isEmpty{password=defaults.password}
    }
}
enum PatchablePreferences {
    static let keys=["rotationMinutes","retries","startupTimeout","qualityMbps","random","validation","unattended","autoLogin","surfCategory"]
    static func snapshot(_ s:Settings)->[String:String]{
        ["rotationMinutes":String(s.rotationMinutes),"retries":String(s.retries),"startupTimeout":String(s.startupTimeout),
         "qualityMbps":String(s.qualityMbps),"random":String(s.random),"validation":String(s.validation),
         "unattended":String(s.unattended),"autoLogin":String(s.autoLogin),"surfCategory":s.surfCategory]
    }
    static func applying(_ changes:[String:String],to settings:Settings)throws->Settings {
        guard !changes.isEmpty,changes.keys.allSatisfy({keys.contains($0)}) else{throw AppError.message("Unknown or empty preference change")}
        var s=settings
        for (key,value) in changes {
            func integer(_ range:ClosedRange<Int>)throws->Int{
                guard let n=Int(value),range.contains(n) else{throw AppError.message("Invalid "+key)}
                return n
            }
            func boolean()throws->Bool{
                guard ["true","false"].contains(value) else{throw AppError.message("Invalid "+key)}
                return value=="true"
            }
            switch key {
            case "rotationMinutes":s.rotationMinutes=try integer(1...1440)
            case "retries":s.retries=try integer(1...5)
            case "startupTimeout":s.startupTimeout=try integer(5...120)
            case "qualityMbps":s.qualityMbps=try integer(1...50)
            case "random":s.random=try boolean()
            case "validation":s.validation=try boolean()
            case "unattended":s.unattended=try boolean()
            case "autoLogin":s.autoLogin=try boolean()
            case "surfCategory":
                guard ["All Channels","Favorites","Sports","Movies & Series"].contains(value) else{throw AppError.message("Invalid Surf category")}
                s.surfCategory=value
            default:throw AppError.message("Unsupported preference")
            }
        }
        return s
    }
}
enum JSONCodec {
    static func encoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .custom { date,encoder in
            let f=ISO8601DateFormatter();f.formatOptions=[.withInternetDateTime,.withFractionalSeconds]
            var c=encoder.singleValueContainer();try c.encode(f.string(from:date))
        }
        e.outputFormatting = [.sortedKeys]; return e
    }
    static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let c = try decoder.singleValueContainer()
            let s = try c.decode(String.self)
            guard let v = date(s) else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "Invalid timestamp") }
            return v
        }
        return d
    }
    static func date(_ s: String?) -> Date? {
        guard let s = s else { return nil }
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]; return f.date(from: s)
    }
}
enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let s) = self { return s }; return nil }
}

// A cycle is scoped to the server, account and category, not the process.
struct SurfRotationState: Codable, Equatable {
    var visited: [String:Set<String>] = [:]
    // Optional for decoding rotation state from previous IPAs.
    var cycles:[String:Int]?=nil
    func progress(scope:String,ids:[String])->(cycle:Int,attempted:Int,remaining:Int){
        let unique=Set(ids)
        let attempted=unique.intersection(visited[scope] ?? []).count
        return (cycles?[scope] ?? 1,attempted,unique.count-attempted)
    }
    mutating func candidates(scope:String, ids:[String], current:String?) -> [String] {
        var remaining=ids.filter{!(visited[scope] ?? []).contains($0)}
        if remaining.isEmpty && !ids.isEmpty {
            visited[scope]=[]
            var updated=cycles ?? [:];updated[scope]=(updated[scope] ?? 1)+1;cycles=updated
            remaining=ids
        }
        let others=remaining.filter{$0 != current}
        return others.isEmpty ? remaining : others
    }
    mutating func record(scope:String,id:String) {
        visited[scope,default:[]].insert(id)
    }
    mutating func seed(scope:String,ids:Set<String>) {
        if visited[scope]==nil {visited[scope]=ids}
    }
}
struct SustainedPlaybackEvidence {
    private(set) var progress=0.0
    private(set) var observed=0.0
    private(set) var interrupted=false
    mutating func observe(delta:Double,elapsed:Double,playing:Bool) {
        guard elapsed>0,elapsed<=5,playing,delta.isFinite,delta>=0 else{interrupted=true;return}
        observed+=elapsed
        progress+=min(delta,elapsed*1.5)
    }
    func qualifies(seconds:Double)->Bool {
        !interrupted && observed>=max(3,seconds-2) && progress>=max(3,seconds*0.7)
    }
}
enum PlaybackFailurePolicy {
    // Strict mode records completed playback failures regardless of cause.
    // Cancellation and intentional pause never incur a penalty.
    static func confirms(serverHealthy:Bool, recentSuccess:Bool, consecutiveFailures:Int,
                         reason:String, cancelled:Bool=false, paused:Bool=false, strict:Bool=false)->Bool {
        guard !cancelled, !paused else { return false }
        if strict { return true }
        guard serverHealthy else { return false }
        let r=reason.lowercased()
        let infrastructure=["http 401","http 403","http 429","http 5","network","not connected","offline","cancelled","canceled","certificate","connection refused","dns","authentication","no media source"]
        if infrastructure.contains(where:{r.contains($0)}) { return false }
        // A Jellyfin catalogue 404 can mean a stale item ID, not a dead source.
        // Require corroborated recent playback for ambiguous player failures.
        return recentSuccess && consecutiveFailures <= 2 && consecutiveFailures > 0 &&
            (r.contains("startup timeout") || r.contains("player error") || r.contains("avplayer item failed") || r.contains("stream http 404") || r.contains("stream http 410"))
    }
}
enum CommandQueue {
    static func decode(_ data:Data,expectedID:String)throws->Command{
        let command=try JSONCodec.decoder().decode(Command.self,from:data)
        guard command.id==expectedID,command.schemaVersion==1 else{throw AppError.message("Command identity or schema mismatch")}
        return command
    }
    static func pending(_ commands:[String],receipts:Set<String>,limit:Int)->[String] {
        Array(Set(commands).subtracting(receipts).sorted().prefix(max(0,limit)))
    }
}
enum SurfCommandSemantics {
    static func rotationOwner(surfing:Bool,shared:Bool)->String {
        !surfing ? "none" : (shared ? "server" : "device")
    }
    static func requireRotationControl(surfing:Bool,shared:Bool)throws {
        guard surfing else { throw AppError.message("No active Surf rotation") }
        guard !shared else {
            throw AppError.message("Rotation is server-owned; pause or resume the local player instead")
        }
    }
}
enum SurfClock {
    static func remaining(deadline:Date?,now:Date)->Int {
        guard let deadline=deadline else{return 0}
        return max(0,Int(ceil(deadline.timeIntervalSince(now))))
    }
    static func resumed(now:Date,remaining:Int)->Date {
        now.addingTimeInterval(Double(max(1,remaining)))
    }
}

// Continuity tests must preserve a healthy validated stream; the cold-start
// playback step always allocates a fresh session.
enum CampaignPlaybackPolicy {
    static func reuse(kind:String,requestedID:String,currentID:String?,validated:Bool,hasItem:Bool,hasSession:Bool,playing:Bool,paused:Bool,hasError:Bool)->Bool {
        ["pause_resume","endurance","background"].contains(kind) &&
        requestedID==currentID && validated && hasItem && hasSession && playing && !paused && !hasError
    }
}
struct VideoEvidence {
    private(set) var frameCount=0
    private(set) var lastFrameTime:Double?
    private(set) var lastFrameObservedAt:Double?
    mutating func observe(frameTime:Double,monotonic:Double){
        guard frameTime.isFinite,monotonic.isFinite else{return}
        if let last=lastFrameTime,abs(last-frameTime)<0.000001{return}
        frameCount+=1;lastFrameTime=frameTime;lastFrameObservedAt=monotonic
    }
    func qualifies(progress:Double,monotonic:Double)->Bool{
        guard let seen=lastFrameObservedAt,monotonic>=seen else{return false}
        return progress.isFinite && progress>=3 && frameCount>=3 && monotonic-seen<=2
    }
}

struct TelemetryCadence {
    let interval: TimeInterval
    private(set) var lastSample: TimeInterval?
    init(interval: TimeInterval = 15) { self.interval = max(1, interval) }
    mutating func shouldSample(at monotonic: TimeInterval, force: Bool = false) -> Bool {
        guard monotonic.isFinite, monotonic >= 0 else { return false }
        if !force, let previous = lastSample, monotonic >= previous, monotonic - previous < interval { return false }
        lastSample = monotonic
        return true
    }
}

enum RemoteControlMode:String,Codable,CaseIterable {
    case lan, github
}
enum LANHTTP {
    static let maxHeader=16*1024
    static let maxBody=4*1024*1024
    enum Parsed {
        case incomplete
        case invalid(String)
        case request(method:String,path:String,body:Data)
    }
    static func authorized(_ buffer:Data,token:String?)->Bool{
        guard let token=token,!token.isEmpty else{return true}
        guard let end=buffer.range(of:Data("\r\n\r\n".utf8)),
              let header=String(data:buffer[..<end.lowerBound],encoding:.utf8) else{return false}
        let values=header.components(separatedBy:"\r\n").dropFirst().compactMap{line->String? in
            guard let colon=line.firstIndex(of:":"),line[..<colon].lowercased()=="authorization" else{return nil}
            return line[line.index(after:colon)...].trimmingCharacters(in:.whitespaces)
        }
        guard values.count==1 else{return false}
        let a=Array(values[0].utf8),b=Array(("Bearer "+token).utf8)
        guard a.count==b.count else{return false}
        var difference:UInt8=0
        for i in a.indices{difference |= a[i] ^ b[i]}
        return difference==0
    }
    static func parse(_ buffer:Data)->Parsed {
        guard buffer.count<=maxHeader+maxBody else{return .invalid("413 Payload Too Large")}
        guard let end=buffer.range(of:Data("\r\n\r\n".utf8)) else {
            return buffer.count>maxHeader ? .invalid("431 Request Header Fields Too Large") : .incomplete
        }
        guard end.upperBound<=maxHeader,let header=String(data:buffer[..<end.lowerBound],encoding:.utf8) else{return .invalid("400 Bad Request")}
        let lines=header.components(separatedBy:"\r\n")
        let first=(lines.first ?? "").split(separator:" ",omittingEmptySubsequences:false)
        guard first.count==3,["HTTP/1.0","HTTP/1.1"].contains(String(first[2])),
              first[1].hasPrefix("/"),!first[1].contains("?"),!first[1].contains("#") else{return .invalid("400 Bad Request")}
        var lengths:[Int]=[]
        for line in lines.dropFirst(){
            guard let colon=line.firstIndex(of:":") else{return .invalid("400 Bad Request")}
            let key=line[..<colon].lowercased()
            let value=line[line.index(after:colon)...].trimmingCharacters(in:.whitespaces)
            if key=="transfer-encoding"{return .invalid("400 Bad Request")}
            if key=="content-length"{
                guard !value.isEmpty,value.allSatisfy({$0.isASCII && $0.isNumber}),let length=Int(value),length>=0 else{return .invalid("400 Bad Request")}
                lengths.append(length)
            }
        }
        guard lengths.count<=1 else{return .invalid("400 Bad Request")}
        let method=String(first[0]),path=String(first[1])
        guard method=="GET" || method=="POST" else{return .invalid("405 Method Not Allowed")}
        if method=="POST" && lengths.isEmpty{return .invalid("411 Length Required")}
        let size=lengths.first ?? 0
        guard size<=maxBody else{return .invalid("413 Payload Too Large")}
        if method=="GET" && size != 0{return .invalid("400 Bad Request")}
        guard buffer.count-end.upperBound>=size else{return .incomplete}
        return .request(method:method,path:path,body:Data(buffer[end.upperBound..<(end.upperBound+size)]))
    }
}

// Same configured origin on HTTPS, or explicit private IPv4 port pairing.
// No arbitrary host, protocol, URL or authentication forwarding is allowed.
struct GatewayConfiguration:Codable,Equatable{
    var lanServerPort:Int?=nil
    var lanGatewayPort:Int?=nil
    var routePrefix:String?=nil
    var ticketField:String?=nil
    var timeoutSeconds:Int?=nil
    func checked()throws->Self{
        let path=routePrefix ?? "/api/surf"
        guard (1...65535).contains(lanServerPort ?? 8096),(1...65535).contains(lanGatewayPort ?? 8097),
              path.hasPrefix("/"),JellyfinAdapterRules.validPath(String(path.dropFirst())),
              (ticketField ?? "url").range(of:"^[A-Za-z_][A-Za-z0-9_]{0,79}$",options:.regularExpression) != nil,
              (5...90).contains(timeoutSeconds ?? 20) else{throw AppError.message("Invalid shared gateway configuration")}
        return self
    }
    func path(mode:String,leaf:String)->String{(routePrefix ?? "/api/surf")+"/"+mode+"/hls/"+leaf}
}

// Same-origin IPTV policy contract. Never sums native and gateway penalties:
// a single terminal event can be observed by both systems.
struct GatewayChannelHealth:Codable,Equatable{
    let lastAttempt:Double
    let hiatusUntil:Double
    let banned:Bool
    let failures:Int
    let lastFailure:Double
    let reason:String
    let successes:Int
    let approvedAt:Double?
    var lastSuccess:Double?=nil
    var valid:Bool{
        [lastAttempt,hiatusUntil,lastFailure,approvedAt ?? 0,lastSuccess ?? 0].allSatisfy{$0.isFinite && $0>=0}
        && (0...20).contains(failures) && successes>=0 && reason.utf8.count<=2048
    }
    func merge(_ native:ChannelHealth)->ChannelHealth{
        var n=native
        let approval=Date(timeIntervalSince1970:approvedAt ?? 0)
        if (approvedAt ?? 0)>0,approval>(n.lastFailure ?? .distantPast),approval>(n.approvedAt ?? .distantPast){
            n.banned=false;n.failures=0;n.hiatusUntil=nil;n.approvedAt=approval
        }
        let nativeApproval=n.approvedAt?.timeIntervalSince1970 ?? 0
        if lastFailure>nativeApproval && (banned || lastFailure >= (n.lastSuccess?.timeIntervalSince1970 ?? 0)){
            n.failures=max(n.failures,failures)
            n.lifetimeFailures=max(n.lifetimeFailures,failures)
            n.banned=n.banned || banned
            if hiatusUntil>0{
                let until=Date(timeIntervalSince1970:hiatusUntil)
                n.hiatusUntil=max(n.hiatusUntil ?? .distantPast,until)
            }
            if lastFailure>0,Date(timeIntervalSince1970:lastFailure)>=(n.lastFailure ?? .distantPast){
                n.lastFailure=Date(timeIntervalSince1970:lastFailure);n.reason=reason
            }
        }
        if let stamp=lastSuccess,stamp>0{
            let success=Date(timeIntervalSince1970:stamp)
            if !n.banned,success>(n.lastFailure ?? .distantPast),success>(n.lastSuccess ?? .distantPast){
                n.failures=0;n.hiatusUntil=nil;n.lastSuccess=success
            }
        }
        n.successes=max(n.successes,successes)
        return n
    }
}
struct GatewayHealthSnapshot:Codable,Equatable{
    let revision:String
    let channels:[String:GatewayChannelHealth]
    var penalty:HealthPenaltyPolicy?=nil
    func checked()throws->Self{
        _ = try penalty?.checked()
        guard revision=="sqlite-policy-v2",!channels.isEmpty,channels.count<=10000,
              channels.allSatisfy({!$0.key.isEmpty && $0.key.utf8.count<=128 && $0.value.valid})
        else{throw AppError.message("Invalid gateway health snapshot")}
        return self
    }
    func merging(_ native:[String:ChannelHealth])->[String:ChannelHealth]{
        var merged=native
        for (id,row) in channels where row.failures>0 || row.banned || row.successes>0 || (row.approvedAt ?? 0)>0{
            merged[id]=row.merge(merged[id] ?? ChannelHealth())
        }
        return merged
    }
}
enum GatewayPolicyError:LocalizedError{
    case excluded,unavailable
    var errorDescription:String?{self == .excluded ? "Gateway channel excluded":"Gateway policy unavailable"}
}
enum PlaybackFailureCategory:String{
    case policy,provider,manifestOrSegment,network,decoder,timeout,videoFrozen,unknown
    static func classify(_ reason:String)->Self{
        let r=reason.lowercased()
        if r.contains("gateway policy") || r.contains("gateway channel excluded"){return .policy}
        if r.contains("video frame") || r.contains("stalled"){return .videoFrozen}
        if r.contains("stream http") || r.contains("manifest") || r.contains("segment"){return .manifestOrSegment}
        if r.contains("http") || r.contains("no media source"){return .provider}
        if r.contains("network") || r.contains("offline"){return .network}
        if r.contains("timeout"){return .timeout}
        if r.contains("player error") || r.contains("avplayer"){return .decoder}
        return .unknown
    }
    var definitive:Bool{self == .policy || self == .provider}
}

// Learned deadlines use only successful per-attempt validation durations.
// Never learn from a failed attempt, intentional pause or shared gateway stream.
struct ChannelStartupProfile:Codable,Equatable{
    var count=0
    var mean=0.0
    var deviation=0.0
    var updatedAt=Date.distantPast
    mutating func observe(seconds:Double,at:Date){
        guard seconds.isFinite,(0.5...120).contains(seconds) else{return}
        if count==0{mean=seconds;deviation=0}
        else{let difference=abs(seconds-mean);mean=0.75*mean+0.25*seconds;deviation=0.75*deviation+0.25*difference}
        count=min(10000,count+1);updatedAt=at
    }
    func deadline(baseline:Int,validation:Int)->Int{
        guard count>=3,mean.isFinite,deviation.isFinite,(0.5...120).contains(mean),(0...120).contains(deviation) else{return baseline}
        return min(120,baseline+15,max(validation+1,5,baseline-5,Int(ceil(mean+max(3,2*deviation)))))
    }
}
struct ChannelStartupProfiles:Codable{
    var entries:[String:ChannelStartupProfile]=[:]
    mutating func record(key:String,seconds:Double,at:Date){
        var row=entries[key] ?? ChannelStartupProfile()
        row.observe(seconds:seconds,at:at)
        guard row.count>0 else{return}
        entries[key]=row
        if entries.count>2048,let oldest=entries.min(by:{$0.value.updatedAt<$1.value.updatedAt})?.key{entries.removeValue(forKey:oldest)}
    }
}
