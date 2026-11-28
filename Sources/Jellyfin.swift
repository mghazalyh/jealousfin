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
    private(set) var gatewayHealth:GatewayHealthSnapshot?
    private var requiresGatewayPolicy=false
    private(set) var connectionEpoch=UUID()
    var adapter:JellyfinAPIAdapter?
    var gatewayConfiguration=GatewayConfiguration()
    private(set) var inFlight=0
    var adapterID:String{adapter?.revision.id ?? "native-baseline"}
    private var adapterSpec:JellyfinAdapterSpec{adapter?.spec ?? JellyfinAdapterSpec()}
    let deviceID: String
    var log: ((String, String, [String: String]) -> Void)?
    private let transport:URLSession
    init(deviceID: String,transport:URLSession = .shared) { self.deviceID = deviceID;self.transport=transport }
    var connected: Bool { !token.isEmpty && !userID.isEmpty }
    func invalidateConnection(){
        connectionEpoch=UUID()
        base=nil;token="";userID="";sharedSurfAvailable=false;gatewayHealth=nil;requiresGatewayPolicy=false
    }
    // Authentication alone is not a ready connection. Stage credentials and
    // the validated catalogue together; retry catalogue failures without
    // exposing a half-initialized session to playback or policy callers.
    func connectCatalogue(_ settings:Settings, stillCurrent:()->Bool = {true},
                          retryDelays:[UInt64]=[1_000_000_000,3_000_000_000,8_000_000_000]) async throws -> [Channel] {
        let previousPolicyOrigin=gatewayHealth != nil ? base:nil
        invalidateConnection()
        let epoch=connectionEpoch
        let expectedConfiguration=gatewayConfiguration
        let expectedAdapter=adapter
        let staged=Jellyfin(deviceID:deviceID,transport:transport)
        staged.adapter=adapter;staged.gatewayConfiguration=gatewayConfiguration;staged.log=log
        try await staged.connect(settings)
        if previousPolicyOrigin==staged.base{staged.requiresGatewayPolicy=true}
        for attempt in 0...retryDelays.count {
            try Task.checkCancellation()
            guard stillCurrent(),connectionEpoch==epoch,gatewayConfiguration==expectedConfiguration,adapter === expectedAdapter else{throw CancellationError()}
            do{
                let catalogue=try await staged.channels()
                try Task.checkCancellation()
                guard stillCurrent(),connectionEpoch==epoch,gatewayConfiguration==expectedConfiguration,adapter === expectedAdapter else{throw CancellationError()}
                adoptConnection(from:staged)
                return catalogue
            }catch{
                if error is CancellationError{throw error}
                if let fault=error as? JellyfinConnectionFault,case .authentication=fault{throw error}
                guard attempt<retryDelays.count else{throw error}
                log?("WARN","CATALOGUE_STARTUP_RETRY",["attempt":String(attempt+1),"errorType":String(describing:type(of:error))])
                try await Task.sleep(nanoseconds:retryDelays[attempt])
            }
        }
        throw AppError.message("Channel catalogue unavailable")
    }
    func adoptConnection(from staged:Jellyfin){
        connectionEpoch=UUID()
        base=staged.base;token=staged.token;userID=staged.userID
        sharedSurfAvailable=staged.sharedSurfAvailable;gatewayHealth=staged.gatewayHealth;requiresGatewayPolicy=staged.requiresGatewayPolicy
    }
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
            if [409,503].contains(http.statusCode),let body=try? JSONSerialization.jsonObject(with:data) as? [String:Any],
               let marker=body["JealousfinPolicyError"] as? String {
                if marker=="excluded",http.statusCode==409{throw GatewayPolicyError.excluded}
                if marker=="unavailable",http.statusCode==503{throw GatewayPolicyError.unavailable}
            }
            if let fault=JellyfinConnectionFault.authenticationStatus(http.statusCode,gateway:gatewayHealth != nil){throw fault}
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
            if phase=="transport",let fault=JellyfinConnectionFault.networkError(error){throw fault}
            throw error
        }
    }
    func connect(_ settings: Settings) async throws {
        let s = settings.server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: s), ["http", "https"].contains(u.scheme?.lowercased() ?? ""), u.host != nil else { throw AppError.message("Enter an HTTP or HTTPS server URL") }
        connectionEpoch=UUID()
        let epoch=connectionEpoch
        base = u; token = ""; userID = ""; sharedSurfAvailable=false;gatewayHealth=nil
        requiresGatewayPolicy=[18796,18797].contains(u.port ?? 0)
        let a = try await request("Users/AuthenticateByName", method: "POST", body: ["Username": settings.username, "Pw": settings.password])
        guard connectionEpoch==epoch else{throw CancellationError()}
        let spec=adapterSpec
        guard let t=JellyfinAdapterRules.string(at:spec.tokenPath,in:a),let id=JellyfinAdapterRules.string(at:spec.userIDPath,in:a),!t.isEmpty,!id.isEmpty else{throw AppError.message("Jellyfin authentication failed")}
        token = t; userID = id
        // Probe the authenticated shared gateway without exposing tickets to hot-patch hooks.
        if SharedSurf.gatewayOrigin(for:u,configuration:gatewayConfiguration) != nil {
            do {
                _ = try await sharedSurfTicket(mode:"all")
                sharedSurfAvailable=true
                log?("INFO","SURF_GATEWAY_AVAILABLE",[:])
            } catch {
                // The known production public origin must not silently regress to the broken session path.
                sharedSurfAvailable=gatewayConfiguration==GatewayConfiguration() && u.scheme?.lowercased()=="https" && u.host?.lowercased()=="desktop-hb9oshs.tail1fc8b.ts.net"
                log?("WARN","SURF_GATEWAY_PROBE_FAILED",["errorType":String(describing:type(of:error))])
            }
        }
    }
    private func sharedSurfTicket(mode:String) async throws -> URL {
        guard connected,let server=base,let origin=SharedSurf.gatewayOrigin(for:server,configuration:gatewayConfiguration),
              ["all","movies-series","sports"].contains(mode),
              let endpoint=URL(string:gatewayConfiguration.path(mode:mode,leaf:"ticket"),relativeTo:origin)?.absoluteURL,
              JellyfinAdapterRules.sameOrigin(endpoint,origin)
        else{throw AppError.message("Shared Surf requires an authenticated supported gateway")}
        var request=URLRequest(url:endpoint)
        request.httpMethod="POST";request.timeoutInterval=Double(gatewayConfiguration.timeoutSeconds ?? 20)
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
              let raw=object[gatewayConfiguration.ticketField ?? "url"] as? String else{throw AppError.message("Invalid shared Surf ticket response")}
        return try SharedSurf.ticketURL(raw,origin:origin,mode:mode,configuration:gatewayConfiguration)
    }
    func sharedSurfChannel(category:String,channels:[Channel])->Channel? {
        guard sharedSurfAvailable else{return nil}
        return SharedSurf.channel(for:category,in:channels)
    }
    static func program(_ x: [String: Any]) -> Program? {JellyfinProgrammeParser.program(x)}
    func channels() async throws -> [Channel] {
        guard connected else{throw AppError.message("Jellyfin authentication required")}
        let origin=base,account=userID,credential=token,epoch=connectionEpoch
        let a = try await request("LiveTv/Channels", query: ["UserId":userID,"EnableImages":"true","EnableUserData":"true","Fields":"Genres,Tags","JealousfinIncludePolicy":"true"])
        guard connectionEpoch==epoch,base==origin,userID==account,token==credential else{throw CancellationError()}
        var policy:GatewayHealthSnapshot?
        if let raw=a["JealousfinPolicy"] {
            let snapshot=try decodeGatewayHealth(raw)
            guard let items=a["Items"] as? [[String:Any]],Set(items.compactMap{$0["Id"] as? String})==Set(snapshot.channels.keys)
            else{throw AppError.message("Gateway policy/catalogue mismatch")}
            policy=snapshot
        }else if requiresGatewayPolicy || gatewayHealth != nil{throw GatewayPolicyError.unavailable}
        guard let items = a["Items"] as? [[String: Any]] else { throw AppError.message("Channel catalogue missing") }
        let catalogue:[Channel] = items.compactMap { x in
            guard let id = x["Id"] as? String, let n = x["Name"] as? String else { return nil }
            let p = (x["CurrentProgram"] as? [String: Any]).flatMap(Self.program)
            let genres=x["Genres"] as? [String] ?? []
            let tags=x["Tags"] as? [String] ?? []
            let genreItems=(x["GenreItems"] as? [[String:Any]] ?? []).compactMap{$0["Name"] as? String}
            let hasLogo = !(x["ImageTags"] as? [String:Any] ?? [:]).isEmpty || x["PrimaryImageTag"] != nil
            let favorite=(x["UserData"] as? [String:Any])?["IsFavorite"] as? Bool ?? false
            return Channel(id:id,name:n,number:x["ChannelNumber"] as? String,logo:hasLogo,favorite:favorite,program:p,genres:genres+tags+genreItems)
        }
        guard !catalogue.isEmpty,catalogue.count==items.count,Set(catalogue.map(\.id)).count==catalogue.count,
              catalogue.allSatisfy({!$0.id.isEmpty && !$0.name.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty})
        else{throw AppError.message("Empty or invalid channel catalogue")}
        gatewayHealth=policy
        if policy != nil{requiresGatewayPolicy=true}
        return catalogue
    }
    private func decodeGatewayHealth(_ raw:Any)throws->GatewayHealthSnapshot{
        let data=try JSONSerialization.data(withJSONObject:raw)
        guard data.count<=4*1024*1024 else{throw AppError.message("Gateway policy exceeds size limit")}
        return try JSONDecoder().decode(GatewayHealthSnapshot.self,from:data).checked()
    }
    func refreshGatewayHealth()async throws{
        guard gatewayHealth != nil else{return}
        let origin=base,account=userID,credential=token,epoch=connectionEpoch
        let raw=try await request("Jealousfin/Policy")
        let snapshot=try decodeGatewayHealth(raw)
        guard connectionEpoch==epoch,base==origin,userID==account,token==credential else{throw CancellationError()}
        gatewayHealth=snapshot
    }
    func submitGatewayHealth(channelID:String,trace:String,action:String,reason:String)async throws{
        guard gatewayHealth != nil else{return}
        let origin=base,account=userID,credential=token,epoch=connectionEpoch
        let raw=try await request("Jealousfin/Policy/Event",method:"POST",body:["channelID":channelID,"trace":trace,"action":action,"reason":reason,"approval":action=="approve" ? "explicit":""])
        let snapshot=try decodeGatewayHealth(raw)
        guard connectionEpoch==epoch,base==origin,userID==account,token==credential else{throw CancellationError()}
        gatewayHealth=snapshot
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
