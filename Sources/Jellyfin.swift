import Foundation

struct StreamSession {
    let url: URL
    let itemID: String
    let mediaSourceID: String
    let playSessionID: String
    let liveStreamID: String?
    let method: String
    var isSharedSurf:Bool{method == "SharedSurfHLS"}
}

private final class SurfNoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
}
@MainActor final class Jellyfin {
    var base: URL?
    var token = ""
    var userID = ""
    private(set) var sharedSurfAvailable=false
    var adapter:JellyfinAPIAdapter?
    private(set) var inFlight=0
    var adapterID:String{adapter?.revision.id ?? "native-baseline"}
    private var adapterSpec:JellyfinAdapterSpec{adapter?.spec ?? JellyfinAdapterSpec()}
    let deviceID: String
    var log: ((String, String, [String: String]) -> Void)?
    private let transport:URLSession
    init(deviceID: String,transport:URLSession = .shared) { self.deviceID = deviceID;self.transport=transport }
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
        let operation=JellyfinAdapterRules.operation(path)
        let active=adapter
        let revision=active?.revision.id ?? "native-baseline"
        let spec=active?.spec ?? JellyfinAdapterSpec()
        inFlight+=1;defer{inFlight-=1}
        let start=ProcessInfo.processInfo.systemUptime
        let requestID=UUID().uuidString
        var phase="request_hook"
        do {
            let plan:(path:String,method:String,query:[String:String],body:[String:Any]?)
            if let active=active{plan=try await active.request(operation:operation,path:path,method:method,query:query,body:body)}
            else{plan=(path,method,query,body)}
            phase="transport"
            var r=URLRequest(url:try url(plan.path,plan.query));r.httpMethod=plan.method;r.timeoutInterval=Double(spec.timeoutSeconds)
            if spec.authorizationHeader != "X-Emby-Authorization"{r.setValue(auth,forHTTPHeaderField:"Authorization")}
            if spec.authorizationHeader != "Authorization"{r.setValue(auth,forHTTPHeaderField:"X-Emby-Authorization")}
            r.setValue("application/json",forHTTPHeaderField:"Accept")
            r.setValue(requestID,forHTTPHeaderField:"X-Jealousfin-Request-ID")
            if let payload=plan.body{r.httpBody=try JSONSerialization.data(withJSONObject:payload);r.setValue("application/json",forHTTPHeaderField:"Content-Type")}
            let (data, httpResponse) = try await transport.data(for: r)
            guard let http = httpResponse as? HTTPURLResponse else { throw AppError.message("Invalid HTTP response") }
            log?(http.statusCode < 400 ? "INFO" : "ERROR","HTTP_RESULT",["operation":operation,"endpoint":plan.path,"method":plan.method,"adapter":revision,"requestID":requestID,"status":String(http.statusCode),"bytes":String(data.count),"ms":String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))])
            guard (200..<300).contains(http.statusCode) else { throw AppError.message("Jellyfin HTTP \(http.statusCode)") }
            phase="response_decode"
            let response:[String:Any]
            if data.isEmpty{response=[:]}
            else{
                let parsed=try JSONSerialization.jsonObject(with:data)
                if let object=parsed as? [String:Any]{response=object}
                else if let array=parsed as? [Any]{response=["Items":array]}
                else{throw AppError.message("Unexpected Jellyfin JSON shape")}
            }
            phase="response_hook"
            if let active=active{return try await active.response(operation:operation,response:response)}
            return response
        } catch {
            log?("ERROR","HTTP_FAILURE",["operation":operation,"adapter":revision,"requestID":requestID,"phase":phase,"errorType":String(describing:type(of:error))])
            if phase=="request_hook" || phase=="response_hook"{throw AppError.message("Jellyfin API adapter failed during "+operation+" "+phase)}
            throw error
        }
    }
    func connect(_ settings: Settings) async throws {
        let s = settings.server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: s), ["http", "https"].contains(u.scheme?.lowercased() ?? ""), u.host != nil else { throw AppError.message("Enter an HTTP or HTTPS server URL") }
        base = u; token = ""; userID = ""; sharedSurfAvailable=false
        let a = try await request("Users/AuthenticateByName", method: "POST", body: ["Username": settings.username, "Pw": settings.password])
        let spec=adapterSpec
        guard let t=JellyfinAdapterRules.string(at:spec.tokenPath,in:a),let id=JellyfinAdapterRules.string(at:spec.userIDPath,in:a),!t.isEmpty,!id.isEmpty else{throw AppError.message("Jellyfin authentication failed")}
        token = t; userID = id
        // Probe the authenticated shared gateway without exposing tickets to hot-patch hooks.
        if SharedSurf.gatewayOrigin(for:u) != nil {
            do {
                _ = try await sharedSurfTicket(mode:"all")
                sharedSurfAvailable=true
                log?("INFO","SURF_GATEWAY_AVAILABLE",[:])
            } catch {
                // The known production public origin must not silently regress to the broken session path.
                sharedSurfAvailable=u.scheme?.lowercased()=="https" && u.host?.lowercased()=="desktop-hb9oshs.tail1fc8b.ts.net"
                log?("WARN","SURF_GATEWAY_PROBE_FAILED",["errorType":String(describing:type(of:error))])
            }
        }
    }
    private func sharedSurfTicket(mode:String) async throws -> URL {
        guard connected,let server=base,let origin=SharedSurf.gatewayOrigin(for:server),
              ["all","movies-series","sports"].contains(mode),
              let endpoint=URL(string:"/api/surf/"+mode+"/hls/ticket",relativeTo:origin)?.absoluteURL,
              JellyfinAdapterRules.sameOrigin(endpoint,origin)
        else{throw AppError.message("Shared Surf requires an authenticated supported gateway")}
        var request=URLRequest(url:endpoint)
        request.httpMethod="POST";request.timeoutInterval=20
        request.setValue(auth,forHTTPHeaderField:"Authorization")
        request.setValue(token,forHTTPHeaderField:"X-Emby-Token")
        request.setValue("application/json",forHTTPHeaderField:"Content-Type")
        request.setValue("application/json",forHTTPHeaderField:"Accept")
        request.httpBody=Data("{}".utf8)
        inFlight+=1;defer{inFlight-=1}
        let (data,response)=try await transport.data(for:request,delegate:SurfNoRedirect())
        guard let http=response as? HTTPURLResponse,let final=http.url,
              JellyfinAdapterRules.sameOrigin(final,origin),http.statusCode==200
        else{throw AppError.message("Shared Surf ticket HTTP "+String((response as? HTTPURLResponse)?.statusCode ?? 0))}
        guard data.count<=65536,let object=try JSONSerialization.jsonObject(with:data) as? [String:Any],
              let raw=object["url"] as? String else{throw AppError.message("Invalid shared Surf ticket response")}
        return try SharedSurf.ticketURL(raw,origin:origin,mode:mode)
    }
    func sharedSurfChannel(category:String,channels:[Channel])->Channel? {
        guard sharedSurfAvailable else{return nil}
        return SharedSurf.channel(for:category,in:channels)
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
            let genres=x["Genres"] as? [String] ?? []
            let tags=x["Tags"] as? [String] ?? []
            let genreItems=(x["GenreItems"] as? [[String:Any]] ?? []).compactMap{$0["Name"] as? String}
            let hasLogo = !(x["ImageTags"] as? [String:Any] ?? [:]).isEmpty || x["PrimaryImageTag"] != nil
            let favorite=(x["UserData"] as? [String:Any])?["IsFavorite"] as? Bool ?? false
            return Channel(id:id,name:n,number:x["ChannelNumber"] as? String,logo:hasLogo,favorite:favorite,program:p,genres:genres+tags+genreItems)
        }
    }
    func programs() async throws -> [Program] {
        let f = ISO8601DateFormatter()
        let a = try await request("LiveTv/Programs", query: ["UserId":userID,"MinEndDate":f.string(from:Date()),"MaxStartDate":f.string(from:Date().addingTimeInterval(7200))])
        return (a["Items"] as? [[String: Any]] ?? []).compactMap(Self.program)
    }
    func logo(_ channel: Channel) -> URL? {
        guard channel.logo else { return nil }
        guard JellyfinAdapterRules.validPath(channel.id),!channel.id.contains("/") else{return nil}
        return try? url(adapterSpec.logoPath.replacingOccurrences(of:"{id}",with:channel.id),["maxWidth":"160","quality":"80","api_key":token])
    }
    func favorite(_ id: String, value: Bool) async throws {
        _ = try await request("Users/\(userID)/FavoriteItems/\(id)", method: value ? "POST" : "DELETE")
    }
    func stream(_ channel: Channel, mbps: Int, playback:PlaybackAttemptPolicy = PlaybackAttemptPolicy()) async throws -> StreamSession {
        if sharedSurfAvailable,let mode=SharedSurf.mode(for:channel) {
            let url=try await sharedSurfTicket(mode:mode)
            log?("INFO","SURF_GATEWAY_TICKET",["mode":mode])
            return StreamSession(url:url,itemID:channel.id,mediaSourceID:channel.id,playSessionID:UUID().uuidString,liveStreamID:nil,method:"SharedSurfHLS")
        }
        let bitrate = max(1, min(50, mbps)) * 1_000_000
        var transcode:[String:Any]=["Container":playback.segmentContainer=="server" ? "ts" : playback.segmentContainer,"Type":"Video","Protocol":"hls","VideoCodec":"h264","AudioCodec":"aac","Context":"Streaming","MaxAudioChannels":"2","MinSegments":playback.minimumSegments>0 ? playback.minimumSegments : 2]
        if playback.segmentSeconds>0{transcode["SegmentLength"]=playback.segmentSeconds}
        let profile: [String: Any] = [
            "Name":"Jealousfin AVPlayer","MaxStreamingBitrate":bitrate,
            "DirectPlayProfiles":[["Container":"mp4,m4v,mov","Type":"Video","VideoCodec":"h264,hevc","AudioCodec":"aac,mp3,ac3,eac3"]],
            "TranscodingProfiles":[transcode],
            "CodecProfiles":[], "SubtitleProfiles":[["Format":"vtt","Method":"Hls"]]
        ]
        var negotiation:[String:Any]=["UserId":userID,"MaxStreamingBitrate":bitrate,"EnableDirectPlay":false,"EnableDirectStream":playback.preferDirect,"EnableTranscoding":true,"AutoOpenLiveStream":true,"DeviceProfile":profile]
        if playback.forceEncode {
            negotiation["AllowVideoStreamCopy"]=false
            negotiation["AllowAudioStreamCopy"]=false
        }
        let info = try await request("Items/\(channel.id)/PlaybackInfo",method:"POST",query:["UserId":userID,"IsPlayback":"true","AutoOpenLiveStream":"true"],body:negotiation)
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
        guard let origin=base,JellyfinAdapterRules.sameOrigin(resolved,origin),
            var c=URLComponents(url:resolved,resolvingAgainstBaseURL:false) else{throw AppError.message("Invalid or non-server playback URL")}
        // Supply the hook a token-free path and add the actual token natively.
        let credentialKeys:Set<String>=["api_key","access_token","token"]
        c.queryItems=c.queryItems?.filter{!credentialKeys.contains($0.name.lowercased())}
        if let active=adapter {
            let path=c.percentEncodedPath+(c.percentEncodedQuery.map{"?"+$0} ?? "")
            let patched=try await active.stream(path:path,method:method,itemID:channel.id)
            guard let u=URL(string:patched.0,relativeTo:origin)?.absoluteURL,JellyfinAdapterRules.sameOrigin(u,origin),
                let parts=URLComponents(url:u,resolvingAgainstBaseURL:false) else{throw AppError.message("Patched stream must target configured Jellyfin")}
            c=parts;method=patched.1
        }
        var q=(c.queryItems ?? []).filter{!credentialKeys.contains($0.name.lowercased())}
        q.append(URLQueryItem(name:"api_key",value:token));c.queryItems=q
        guard let result=c.url else{throw AppError.message("Invalid playback URL")}
        return StreamSession(url:try playback.applying(to:result),itemID:channel.id,mediaSourceID:sid,playSessionID:playID,liveStreamID:liveID,method:method)
    }
    func hlsReadiness(_ s:StreamSession,budget:TimeInterval) async -> HLSReadinessReport {
        guard !s.isSharedSurf,let origin=base,s.url.path.lowercased().hasSuffix(".m3u8") else {
            return HLSReadinessReport(outcome:.cancelled,reason:"Diagnostic only supports ordinary HLS")
        }
        return await HLSReadinessProbe.waitReady(url:s.url,origin:origin,budget:budget)
    }
    func report(_ s: StreamSession, action: String, position: Double, paused: Bool) async {
        // Shared media is not a Jellyfin allocated live-stream session.
        guard !s.isSharedSurf else{return}
        let p = position.isFinite ? max(0,min(position,1e8)) : 0
        var body: [String: Any] = ["ItemId":s.itemID,"MediaSourceId":s.mediaSourceID,"PlaySessionId":s.playSessionID,"PositionTicks":Int64(p*10_000_000),"IsPaused":paused,"PlayMethod":s.method]
        if let id=s.liveStreamID { body["LiveStreamId"]=id }
        do { _ = try await request("Sessions/Playing"+(action.isEmpty ? "" : "/"+action),method:"POST",body:body) }
        catch { log?("WARN","SESSION_REPORT_FAILED",["action":action]) }
    }
    func close(_ s: StreamSession, position: Double) async {
        guard !s.isSharedSurf else{return}
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
