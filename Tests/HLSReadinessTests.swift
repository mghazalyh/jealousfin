import Foundation
private struct Fixture:HLSProbeTransport {
    var answer:(URL,Bool)async throws->HLSProbeReply
    func fetch(_ url:URL,range:Bool,timeout:TimeInterval)async throws->HLSProbeReply{try await answer(url,range)}
}
@main struct HLSReadinessTests {
    static func main()async {
        let origin=URL(string:"http://fixture.invalid:8096")!
        let master=origin.appendingPathComponent("master.m3u8")
        func reply(_ url:URL,_ text:String,_ status:Int=200)->HLSProbeReply{HLSProbeReply(status:status,body:Data(text.utf8),url:url)}
        let fixture=Fixture{url,range in
            switch url.lastPathComponent {
            case "master.m3u8":return reply(url,"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant.m3u8?api_key=fixture-secret")
            case "variant.m3u8":return reply(url,"#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:3,\nseg.mp4")
            default:precondition(range);return reply(url,"xx",206)
            }
        }
        let ok=await HLSReadinessProbe.waitReady(url:master,origin:origin,budget:1,transport:fixture)
        precondition(ok.outcome == .ready && ok.evidence["initStatus"]=="206" && ok.evidence["segmentStatus"]=="206")
        precondition(!String(describing:ok.logFields).contains("fixture-secret"))
        let outside=Fixture{url,_ in
            precondition(url==master)
            return reply(url,"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttp://other.invalid/variant.m3u8")
        }
        let blocked=await HLSReadinessProbe.waitReady(url:master,origin:origin,budget:1,transport:outside)
        precondition(blocked.outcome == .failed)
        let pending=Fixture{url,_ in reply(url,"#EXTM3U\n#EXT-X-TARGETDURATION:3")}
        let noSegments=await HLSReadinessProbe.waitReady(url:master,origin:origin,budget:0.1,transport:pending)
        precondition(noSegments.outcome == .notReady)
        let denied=Fixture{url,_ in reply(url,"",403)}
        let forbidden=await HLSReadinessProbe.waitReady(url:master,origin:origin,budget:1,transport:denied)
        precondition(forbidden.outcome == .failed && forbidden.evidence["masterStatus"]=="403")
        let slow=Fixture{url,_ in try await Task.sleep(nanoseconds:5_000_000_000);return reply(url,"")}
        let job=Task{await HLSReadinessProbe.waitReady(url:master,origin:origin,budget:10,transport:slow)}
        job.cancel()
        let cancelled=await job.value
        precondition(cancelled.outcome == .cancelled)
        print("PASS: HLS master/variant/init/segment, origin boundary, redaction, pending, authorization and cancellation fixtures")
    }
}
