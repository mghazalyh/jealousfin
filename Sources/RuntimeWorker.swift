import Foundation
import JavaScriptCore

// Run scripts off the UI thread. A timed-out interpreter is isolated until it exits.
private final class ScriptCompletion: @unchecked Sendable {
    private let lock=NSLock()
    private var continuation:CheckedContinuation<(String,[Command]),Error>?
    init(_ c:CheckedContinuation<(String,[Command]),Error>){continuation=c}
    func finish(_ result:Result<(String,[Command]),Error>){
        lock.lock();let c=continuation;continuation=nil;lock.unlock()
        c?.resume(with:result)
    }
}
private final class RuntimeWorker: @unchecked Sendable {
    static let shared=RuntimeWorker()
    private let lock=NSLock()
    private var busy=false
    func run(_ source:String,snapshot:[String:Any]) async throws -> (String,[Command]){
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            guard !busy else{lock.unlock();continuation.resume(throwing:AppError.message("Script interpreter busy"));return}
            busy=true;lock.unlock()
            let completion=ScriptCompletion(continuation)
            DispatchQueue.global(qos:.utility).asyncAfter(deadline:.now()+5){
                completion.finish(.failure(AppError.message("Script exceeded five-second execution deadline")))
            }
            DispatchQueue.global(qos:.utility).async {
                defer{self.lock.lock();self.busy=false;self.lock.unlock()}
                do {
                    guard source.utf8.count<=32768,let context=JSContext() else{throw AppError.message("Invalid script")}
                    var commands:[Command]=[];var logs:[String]=[]
                    let emit:@convention(block)(String,String)->Void={name,json in
                        guard commands.count<100,let d=json.data(using:.utf8),let a=(try? JSONSerialization.jsonObject(with:d)) as? [String:String] else{return}
                        commands.append(Command(id:UUID().uuidString,action:name,arguments:a,expiresAt:nil))
                    }
                    let log:@convention(block)(String)->Void={v in if logs.count<100{logs.append(String(v.prefix(2048)))}}
                    context.setObject(emit,forKeyedSubscript:"nativeCommand" as NSString)
                    context.setObject(log,forKeyedSubscript:"nativeLog" as NSString)
                    context.setObject(snapshot,forKeyedSubscript:"snapshot" as NSString)
                    context.evaluateScript("var api={state:snapshot,command:function(n,a){nativeCommand(n,JSON.stringify(a||{}));},log:function(v){nativeLog(String(v));}};var console={log:api.log};")
                    let result=context.evaluateScript(source)
                    if let e=context.exception{throw AppError.message("JavaScript exception: "+String((e.toString() ?? "unknown").prefix(500)))}
                    completion.finish(.success(((logs+[result?.toString() ?? "undefined"]).joined(separator:"\n"),commands)))
                }catch{completion.finish(.failure(error))}
            }
        }
    }
}
extension ScriptEngine {
    func evaluateAsync(_ source:String,snapshot:[String:Any]) async throws -> (String,[Command]){
        try await RuntimeWorker.shared.run(source,snapshot:snapshot)
    }
}
