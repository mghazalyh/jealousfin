import Foundation

private struct InterpreterEnvelope: Decodable {
    struct Action: Decodable {
        var action: String
        var arguments: [String:String]
    }
    var result: String
    var logs: [String]
    var commands: [Action]
}

// Every evaluation gets a fresh, interruptible interpreter. No JIT or OS modules.
private final class RuntimeWorker: @unchecked Sendable {
    static let shared=RuntimeWorker()
    private let queue=DispatchQueue(label:"jealousfin.interpreter",qos:.utility)
    private let lock=NSLock()
    private var waiting=0

    func run(_ source:String,snapshot:[String:Any]) async throws -> (String,[Command]){
        guard source.utf8.count<=256*1024 else{throw AppError.message("Script exceeds size limit")}
        let data=try JSONSerialization.data(withJSONObject:snapshot,options:[.sortedKeys])
        guard data.count<=1048576 else{throw AppError.message("Snapshot exceeds interpreter limit")}
        let sourceData=try JSONSerialization.data(withJSONObject:source,options:[.fragmentsAllowed])
        let input=String(decoding:data,as:UTF8.self)
        let script=String(decoding:sourceData,as:UTF8.self)
        let wrapper="""
        (function(){
            const snapshot = \(input);
            const logs = [], commands = [];
            globalThis.snapshot = snapshot;
            globalThis.api = {
                state: snapshot,
                command: function(name,args) {
                    if (commands.length >= 100) throw new Error("Command limit");
                    const values = args || {};
                    if (Object.keys(values).some(k => typeof values[k] !== "string")) {
                        throw new Error("Command argument values must be strings");
                    }
                    commands.push({action:String(name), arguments:values});
                },
                log: function(value) {
                    if (logs.length < 100) logs.push(String(value).slice(0,2048));
                }
            };
            globalThis.console = {log:api.log};
            const value = (0,eval)(\(script));
            return JSON.stringify({result:String(value), logs:logs, commands:commands});
        })()
        """
        return try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            guard waiting<4 else {
                lock.unlock();continuation.resume(throwing:AppError.message("Interpreter queue full"));return
            }
            waiting+=1;lock.unlock()
            queue.async {
                defer{self.lock.lock();self.waiting-=1;self.lock.unlock()}
                do {
                    var status:Int32=1
                    guard let ptr=wrapper.withCString({jf_runtime_eval($0,5000,&status)}) else{throw AppError.message("Interpreter allocation failed")}
                    defer{jf_runtime_free(ptr)}
                    let raw=String(cString:ptr)
                    guard status==0 else{throw AppError.message(String(raw.prefix(4096)))}
                    let envelope=try JSONDecoder().decode(InterpreterEnvelope.self,from:Data(raw.utf8))
                    let commands=envelope.commands.map{Command(id:UUID().uuidString,action:$0.action,arguments:$0.arguments,expiresAt:nil)}
                    continuation.resume(returning:((envelope.logs+[envelope.result]).joined(separator:"\n"),commands))
                }catch{continuation.resume(throwing:error)}
            }
        }
    }
}
extension ScriptEngine {
    func evaluateAsync(_ source:String,snapshot:[String:Any]) async throws -> (String,[Command]){
        try await RuntimeWorker.shared.run(source,snapshot:snapshot)
    }
}
