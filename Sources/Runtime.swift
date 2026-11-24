import Foundation
import JavaScriptCore
import Network

// Scripts execute application APIs, not an operating-system shell.
// JavaScriptCore is interpreted on the target device; native changes still require an IPA.
@MainActor final class ScriptEngine {
    func evaluate(_ script:String,snapshot:[String:Any]) throws -> (String,[Command]) {
        guard script.utf8.count<=32768, let context=JSContext() else { throw AppError.message("Script unavailable or too large") }
        var commands:[Command]=[]
        var logs:[String]=[]
        let emit: @convention(block) (String,String) -> Void = { name,json in
            guard commands.count<100, let d=json.data(using:.utf8), let args=(try? JSONSerialization.jsonObject(with:d)) as? [String:String] else { return }
            commands.append(Command(id:UUID().uuidString,action:name,arguments:args,expiresAt:nil))
        }
        let log: @convention(block) (String) -> Void = { value in if logs.count<100 { logs.append(String(value.prefix(2048))) } }
        context.setObject(emit,forKeyedSubscript:"nativeCommand" as NSString)
        context.setObject(log,forKeyedSubscript:"nativeLog" as NSString)
        context.setObject(snapshot,forKeyedSubscript:"snapshot" as NSString)
        context.evaluateScript("""
        var api = { state: snapshot, command: function(name,args) { nativeCommand(name,JSON.stringify(args||{})); },
        log: function(value) { nativeLog(String(value)); } };
        var console = { log: api.log };
        """)
        // No synchronous scripts from untrusted sources; the local owner controls the script.
        let result=context.evaluateScript(script)
        if let exception=context.exception { throw AppError.message("JavaScript exception: "+String((exception.toString() ?? "unknown").prefix(500))) }
        return ((logs+[result?.toString() ?? "undefined"]).joined(separator:"\n"),commands)
    }
}

// Small HTTP/1.1 command transport. No shell or file-system endpoint.
final class LANServer {
    private var listener:NWListener?
    private let queue=DispatchQueue(label:"jealousfin.lan")
    var handler:((Data,@escaping(Data)->Void)->Void)?
    var state:((String)->Void)?
    func start() throws {
        guard listener==nil else { return }
        let l=try NWListener(using:.tcp,on:8765)
        l.stateUpdateHandler={ [weak self] status in self?.state?(String(describing:status)) }
        l.newConnectionHandler={ [weak self] c in
            c.start(queue:self?.queue ?? .global())
            self?.receive(c,buffer:Data())
        }
        listener=l; l.start(queue:queue)
    }
    func stop(){ listener?.cancel(); listener=nil }
    private func respond(_ c:NWConnection,_ status:String,_ data:Data) {
        let head="HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(data.count)\r\nConnection: close\r\n\r\n"
        var packet=Data(head.utf8); packet.append(data)
        c.send(content:packet,completion:.contentProcessed{_ in c.cancel()})
    }
    private func receive(_ c:NWConnection,buffer:Data) {
        c.receive(minimumIncompleteLength:1,maximumLength:65536){[weak self] data,_,done,error in
            guard let self=self else { c.cancel(); return }
            var b=buffer; if let data=data { b.append(data) }
            guard b.count<=131072,error==nil else { c.cancel(); return }
            if let end=b.range(of:Data("\r\n\r\n".utf8)),let header=String(data:b[..<end.lowerBound],encoding:.utf8) {
                let lines=header.components(separatedBy:"\r\n")
                let first=lines.first ?? ""
                if first.hasPrefix("GET /health ") { self.respond(c,"200 OK",Data("{\"service\":\"Jealousfin\",\"status\":\"reachable\"}".utf8)); return }
                guard first.hasPrefix("POST /command ") else { self.respond(c,"404 Not Found",Data("{}".utf8)); return }
                let len=lines.compactMap { line -> Int? in
                    guard line.lowercased().hasPrefix("content-length:") else { return nil }
                    return Int(line.dropFirst(15).trimmingCharacters(in:.whitespaces))
                }.first ?? -1
                guard len>=0,len<=65536 else { self.respond(c,"400 Bad Request",Data("{}".utf8)); return }
                if b.count-end.upperBound>=len {
                    let body=Data(b[end.upperBound..<(end.upperBound+len)])
                    guard let handler=self.handler else { self.respond(c,"503 Unavailable",Data("{}".utf8)); return }
                    handler(body){ output in self.respond(c,"200 OK",output) }; return
                }
            }
            if done { c.cancel() } else { self.receive(c,buffer:b) }
        }
    }
}
