import Foundation

struct StreamSession {
    let url: URL
    let itemID: String
    let mediaSourceID: String
    let playSessionID: String
    let liveStreamID: String?
    let method: String
}
@MainActor final class Jellyfin {
    var base: URL?
    var token = ""
    var userID = ""
    let deviceID: String
    var log: ((String, String, [String: String]) -> Void)?
    init(deviceID: String) { self.deviceID = deviceID }
    var connected: Bool { !token.isEmpty && !userID.isEmpty }
    private var auth: String {
        "MediaBrowser Client=\"Jealousfin\", Device=\"iOS\", DeviceId=\"\(deviceID)\", Version=\"0.1\"" + (token.isEmpty ? "" : ", Token=\"\(token)\"")
    }
    func url(_ path: String, _ query: [String: String] = [:]) throws -> URL {
        guard let base = base, var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false) else { throw AppError.message("Configure Jellyfin server") }
        if !query.isEmpty { c.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) } }
        guard let result = c.url else { throw AppError.message("Invalid server URL") }; return result
    }
    func request(_ path: String, method: String = "GET", query: [String: String] = [:], body: [String: Any]? = nil) async throws -> [String: Any] {
        var r = URLRequest(url: try url(path, query)); r.httpMethod = method; r.timeoutInterval = 20
        r.setValue(auth, forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body = body { r.httpBody = try JSONSerialization.data(withJSONObject: body); r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let start = ProcessInfo.processInfo.systemUptime
        do {
            let (data, response) = try await URLSession.shared.data(for: r)
            guard let http = response as? HTTPURLResponse else { throw AppError.message("Invalid HTTP response") }
            log?(http.statusCode < 400 ? "INFO" : "ERROR", "HTTP_RESULT", ["endpoint": path, "method": method, "status": String(http.statusCode), "bytes": String(data.count), "ms": String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
            guard (200..<300).contains(http.statusCode) else { throw AppError.message("Jellyfin HTTP \(http.statusCode)") }
            if data.isEmpty { return [:] }
            return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
        } catch {
            log?("ERROR", "HTTP_FAILURE", ["endpoint": path, "method": method, "errorType": String(describing: type(of: error))])
            throw error
        }
    }
    func connect(_ settings: Settings) async throws {
        let s = settings.server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: s), ["http", "https"].contains(u.scheme?.lowercased() ?? ""), u.host != nil else { throw AppError.message("Enter an HTTP or HTTPS server URL") }
        base = u; token = ""; userID = ""
        let a = try await request("Users/AuthenticateByName", method: "POST", body: ["Username": settings.username, "Pw": settings.password])
        guard let t = a["AccessToken"] as? String, let user = a["User"] as? [String: Any], let id = user["Id"] as? String, !t.isEmpty else { throw AppError.message("Jellyfin authentication failed") }
        token = t; userID = id
    }
    static func program(_ x: [String: Any]) -> Program? {
        guard let name = x["Name"] as? String, let id = x["Id"] as? String else { return nil }
        return Program(id: id, name: name, start: JSONCodec.date(x["StartDate"] as? String), end: JSONCodec.date(x["EndDate"] as? String), channelID: x["ChannelId"] as? String)
    }
    func channels() async throws -> [Channel] {
        let a = try await request("LiveTv/Channels", query: ["UserId":userID,"EnableImages":"true","EnableUserData":"true","Fields":"Genres,Tags"])
        guard let items = a["Items"] as? [[String: Any]] else { throw AppError.message("Channel catalogue missing") }
        return items.compactMap { x in
            guard let id = x["Id"] as? String, let n = x["Name"] as? String else { return nil }
            let p = (x["CurrentProgram"] as? [String: Any]).flatMap(Self.program)
            return Channel(id: id, name: n, number: x["ChannelNumber"] as? String, logo: !(x["ImageTags"] as? [String: Any] ?? [:]).isEmpty || x["PrimaryImageTag"] != nil, favorite: (x["UserData"] as? [String: Any])?["IsFavorite"] as? Bool ?? false, program: p, genres: (x["Genres"] as? [String] ?? []) + (x["Tags"] as? [String] ?? []) + ((x["GenreItems"] as? [[String:Any]] ?? []).compactMap{$0["Name"] as? String}))
        }
    }
    func programs() async throws -> [Program] {
        let f = ISO8601DateFormatter()
        let a = try await request("LiveTv/Programs", query: ["UserId":userID,"MinEndDate":f.string(from:Date()),"MaxStartDate":f.string(from:Date().addingTimeInterval(7200))])
        return (a["Items"] as? [[String: Any]] ?? []).compactMap(Self.program)
    }
    func logo(_ channel: Channel) -> URL? {
        guard channel.logo else { return nil }
        return try? url("Items/\(channel.id)/Images/Primary", ["maxWidth":"160","quality":"80","api_key":token])
    }
    func favorite(_ id: String, value: Bool) async throws {
        _ = try await request("Users/\(userID)/FavoriteItems/\(id)", method: value ? "POST" : "DELETE")
    }
    func stream(_ channel: Channel, mbps: Int) async throws -> StreamSession {
        let bitrate = max(1, min(50, mbps)) * 1_000_000
        let profile: [String: Any] = [
            "Name":"Jealousfin AVPlayer","MaxStreamingBitrate":bitrate,
            "DirectPlayProfiles":[["Container":"mp4,m4v,mov","Type":"Video","VideoCodec":"h264,hevc","AudioCodec":"aac,mp3,ac3,eac3"]],
            "TranscodingProfiles":[["Container":"ts","Type":"Video","Protocol":"hls","VideoCodec":"h264","AudioCodec":"aac","Context":"Streaming","MaxAudioChannels":"2","MinSegments":2]],
            "CodecProfiles":[], "SubtitleProfiles":[["Format":"vtt","Method":"Hls"]]
        ]
        let info = try await request("Items/\(channel.id)/PlaybackInfo", method:"POST", query:["UserId":userID,"IsPlayback":"true","AutoOpenLiveStream":"true"], body:["UserId":userID,"MaxStreamingBitrate":bitrate,"EnableDirectPlay":true,"EnableDirectStream":true,"EnableTranscoding":true,"AutoOpenLiveStream":true,"DeviceProfile":profile])
        guard let source = (info["MediaSources"] as? [[String:Any]])?.first else { throw AppError.message("No media source returned") }
        let sid = source["Id"] as? String ?? channel.id
        let playID = info["PlaySessionId"] as? String ?? UUID().uuidString
        let liveID = source["LiveStreamId"] as? String
        var resolved: URL
        var method = "Transcode"
        if let path = source["TranscodingUrl"] as? String, !path.isEmpty, let origin = base, let u = URL(string:path,relativeTo:origin)?.absoluteURL {
            resolved = u
        } else {
            var q = ["Static":"true","MediaSourceId":sid,"PlaySessionId":playID]
            if let liveID = liveID { q["LiveStreamId"] = liveID }
            resolved = try url("Videos/\(channel.id)/stream",q); method = "DirectStream"
        }
        guard var c = URLComponents(url:resolved,resolvingAgainstBaseURL:false) else { throw AppError.message("Invalid stream URL") }
        var q = c.queryItems ?? []; if !q.contains(where:{$0.name.lowercased()=="api_key"}) { q.append(URLQueryItem(name:"api_key",value:token)) }; c.queryItems = q
        guard let result = c.url else { throw AppError.message("Invalid playback URL") }
        return StreamSession(url:result,itemID:channel.id,mediaSourceID:sid,playSessionID:playID,liveStreamID:liveID,method:method)
    }
    func report(_ s: StreamSession, action: String, position: Double, paused: Bool) async {
        let p = position.isFinite ? max(0,min(position,1e8)) : 0
        var body: [String: Any] = ["ItemId":s.itemID,"MediaSourceId":s.mediaSourceID,"PlaySessionId":s.playSessionID,"PositionTicks":Int64(p*10_000_000),"IsPaused":paused,"PlayMethod":s.method]
        if let id=s.liveStreamID { body["LiveStreamId"]=id }
        do { _ = try await request("Sessions/Playing"+(action.isEmpty ? "" : "/"+action),method:"POST",body:body) }
        catch { log?("WARN","SESSION_REPORT_FAILED",["action":action]) }
    }
    func close(_ s: StreamSession, position: Double) async {
        await report(s,action:"Stopped",position:position,paused:false)
        if let id=s.liveStreamID {
            do { _ = try await request("LiveStreams/Close",method:"POST",query:["LiveStreamId":id]) }
            catch { log?("WARN","STREAM_CLOSE_FAILED",[:]) }
        }
    }
    func healthy() async -> Bool { (try? await request("System/Info/Public")) != nil }
    func record(_ program: Program) async throws { _ = try await request("LiveTv/Timers",method:"POST",body:["ProgramId":program.id]) }
    func recordings() async throws -> [String] {
        let a=try await request("LiveTv/Recordings",query:["UserId":userID])
        return (a["Items"] as? [[String:Any]] ?? []).compactMap{$0["Name"] as? String}
    }
}
