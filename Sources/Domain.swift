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
    mutating func fail(at date: Date, reason: String) {
        guard eligible(at: date) else { return }
        failures += 1
        lifetimeFailures += 1
        lastFailure = date
        self.reason = reason
        if failures >= 8 { banned = true; hiatusUntil = nil }
        else { hiatusUntil = date.addingTimeInterval(Double(failures * 3) * 86400) }
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
            case "failure": s.fail(at: e.at, reason: e.reason)
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
struct Settings: Codable {
    var server = ""
    var username = ""
    var password = ""
    var githubToken = ""
    var rotationMinutes = 5
    var retries = 2
    var startupTimeout = 25
    var validation = true
    var random = false
    var unattended = true
    var lanEnabled = true
    var qualityMbps = 10
    var autoLogin = true
    var resumeSurf = false
    var surfCategory = "All Channels"
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

enum PlaybackFailurePolicy {
    // A healthy server is necessary but not sufficient. Never punish a channel
    // for a canceled request, authentication failure, network loss or UI pause.
    static func confirms(serverHealthy:Bool, recentSuccess:Bool, consecutiveFailures:Int,
                         reason:String, cancelled:Bool=false, paused:Bool=false)->Bool {
        guard serverHealthy, !cancelled, !paused else { return false }
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
    static func pending(_ commands:[String],receipts:Set<String>,limit:Int)->[String] {
        Array(Set(commands).subtracting(receipts).sorted().prefix(max(0,limit)))
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
