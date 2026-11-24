import Foundation

// Diagnostic-only HLS inspection. It does not alter AVPlayer, forward tokens,
// follow redirects, or decide that a channel is playable.
struct HLSProbeReply {
    let status:Int
    let body:Data
    let url:URL
}
protocol HLSProbeTransport {
    func fetch(_ url:URL,range:Bool,timeout:TimeInterval)async throws->HLSProbeReply
}
private final class HLSRedirectBlocker:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void){completionHandler(nil)}
}
final class NativeHLSProbeTransport:HLSProbeTransport {
    func fetch(_ url:URL,range:Bool,timeout:TimeInterval)async throws->HLSProbeReply {
        let config=URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest=timeout
        config.timeoutIntervalForResource=timeout
        config.httpShouldSetCookies=false
        config.httpCookieStorage=nil
        config.urlCache=nil
        let session=URLSession(configuration:config,delegate:HLSRedirectBlocker(),delegateQueue:nil)
        defer{session.invalidateAndCancel()}
        var req=URLRequest(url:url,cachePolicy:.reloadIgnoringLocalCacheData,timeoutInterval:timeout)
        if range{req.setValue("bytes=0-1",forHTTPHeaderField:"Range")}
        let (bytes,response)=try await session.bytes(for:req)
        defer{bytes.task.cancel()}
        guard let http=response as? HTTPURLResponse,let final=http.url else{throw URLError(.badServerResponse)}
        var data=Data()
        if (200..<300).contains(http.statusCode){
            for try await byte in bytes {
                try Task.checkCancellation()
                data.append(byte)
                if range && data.count>=2{break}
                if data.count>262144{throw URLError(.dataLengthExceedsMaximum)}
            }
        }
        return HLSProbeReply(status:http.statusCode,body:data,url:final)
    }
}
struct HLSReadinessReport {
    enum Outcome:String {case ready,notReady,failed,cancelled}
    var outcome:Outcome
    var reason:String
    var evidence:[String:String]=[:]
    var logFields:[String:String]{
        evidence.merging(["outcome":outcome.rawValue,"reason":reason]){_,new in new}
    }
}
enum HLSReadinessProbe {
    private enum Playlist {case master(String),media(segment:String,map:String?),pending,invalid}
    private static func playlist(_ data:Data)->Playlist {
        guard let text=String(data:data,encoding:.utf8) else{return .invalid}
        let lines=text.components(separatedBy:.newlines).map{$0.trimmingCharacters(in:.whitespacesAndNewlines)}.filter{!$0.isEmpty}
        guard lines.first=="#EXTM3U" else{return .invalid}
        var variant=false,segment=false
        var map:String?
        for line in lines.dropFirst() {
            if line.hasPrefix("#EXT-X-STREAM-INF:"){variant=true;continue}
            if line.hasPrefix("#EXT-X-MAP:"),let a=line.range(of:"URI=\""),let b=line[a.upperBound...].firstIndex(of:"\""){map=String(line[a.upperBound..<b]);continue}
            if line.hasPrefix("#EXTINF:"){segment=true;continue}
            if line.hasPrefix("#"){continue}
            if variant{return .master(line)}
            if segment{return .media(segment:line,map:map)}
        }
        return .pending
    }
    private static func resolve(_ raw:String,relativeTo parent:URL,origin:URL)->URL? {
        guard raw.utf8.count<=16384,let url=URL(string:raw,relativeTo:parent)?.absoluteURL,url.fragment==nil,
              JellyfinAdapterRules.sameOrigin(url,origin) else{return nil}
        return url
    }
    static func waitReady(url:URL,origin:URL,budget:TimeInterval,transport:HLSProbeTransport=NativeHLSProbeTransport())async->HLSReadinessReport {
        var report=HLSReadinessReport(outcome:.notReady,reason:"Playlist readiness not established")
        let start=ProcessInfo.processInfo.systemUptime
        let end=start+max(0.05,min(20,budget))
        func finish(_ outcome:HLSReadinessReport.Outcome,_ reason:String)->HLSReadinessReport {
            report.outcome=outcome;report.reason=reason
            report.evidence["elapsedMs"]=String(Int((ProcessInfo.processInfo.systemUptime-start)*1000))
            return report
        }
        guard JellyfinAdapterRules.sameOrigin(url,origin) else{return finish(.failed,"Initial URL is outside configured origin")}
        func request(_ target:URL,_ stage:String,range:Bool=false)async throws->HLSProbeReply {
            try Task.checkCancellation()
            let left=end-ProcessInfo.processInfo.systemUptime
            guard left>0 else{throw URLError(.timedOut)}
            let response=try await transport.fetch(target,range:range,timeout:min(5,left))
            try Task.checkCancellation()
            report.evidence[stage+"Status"]=String(response.status)
            report.evidence[stage+"Bytes"]=String(response.body.count)
            guard JellyfinAdapterRules.sameOrigin(response.url,origin) else{throw URLError(.redirectToNonExistentLocation)}
            return response
        }
        var cycles=0
        while ProcessInfo.processInfo.systemUptime<end {
            if Task.isCancelled{return finish(.cancelled,"Diagnostic cancelled")}
            cycles+=1;report.evidence["cycles"]=String(cycles)
            do{
                let root=try await request(url,"master")
                guard root.status==200 else{
                    report.reason="Master HTTP response unavailable"
                    if [401,403].contains(root.status){return finish(.failed,report.reason)}
                    throw URLError(.resourceUnavailable)
                }
                var parsed=playlist(root.body),parent=root.url
                if case .master(let uri)=parsed {
                    guard let variant=resolve(uri,relativeTo:parent,origin:origin) else{return finish(.failed,"Variant outside configured origin")}
                    let response=try await request(variant,"variant")
                    guard response.status==200 else{report.reason="Variant HTTP response unavailable";throw URLError(.resourceUnavailable)}
                    parsed=playlist(response.body);parent=response.url
                }
                switch parsed {
                case .media(let uri,let map):
                    if let map=map{
                        guard let initURL=resolve(map,relativeTo:parent,origin:origin) else{return finish(.failed,"Initialization segment outside configured origin")}
                        let initial=try await request(initURL,"init",range:true)
                        guard [200,206].contains(initial.status),!initial.body.isEmpty else{report.reason="Initialization segment unavailable";throw URLError(.resourceUnavailable)}
                    }
                    guard let segment=resolve(uri,relativeTo:parent,origin:origin) else{return finish(.failed,"Media segment outside configured origin")}
                    let response=try await request(segment,"segment",range:true)
                    if [200,206].contains(response.status),!response.body.isEmpty {
                        return finish(.ready,"Playlist and first media bytes reachable; decoding unverified")
                    }
                    report.reason="Media segment unavailable"
                case .pending:report.reason="Media playlist has no complete segment"
                case .invalid:return finish(.failed,"Response is not an HLS playlist")
                case .master:return finish(.failed,"Nested master unsupported by diagnostic")
                }
            }catch{
                if Task.isCancelled || error is CancellationError{return finish(.cancelled,"Diagnostic cancelled")}
                if (error as? URLError)?.code == .redirectToNonExistentLocation{return finish(.failed,"Response outside configured origin")}
                // Only stable categories are recorded; errors may embed URLs.
                if report.reason=="Playlist readiness not established"{report.reason="HLS request unavailable"}
            }
            let remaining=end-ProcessInfo.processInfo.systemUptime
            if remaining>0 {
                do{try await Task.sleep(nanoseconds:UInt64(min(0.5,remaining)*1_000_000_000))}
                catch{return finish(.cancelled,"Diagnostic cancelled")}
            }
        }
        return finish(.notReady,report.reason)
    }
}
