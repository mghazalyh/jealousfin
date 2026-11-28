import Foundation
import Network

// Scripts execute application APIs, not an operating-system shell.
// JavaScriptCore is interpreted on the target device; native changes still require an IPA.
@MainActor final class ScriptEngine {
    var budget=InterpreterBudget()
    private(set) var evaluations=0
    private(set) var failures=0
    private(set) var lastEvaluationMs=0
    func recordEvaluation(start:TimeInterval,failed:Bool){
        evaluations+=1
        if failed{failures+=1}
        lastEvaluationMs=Int((ProcessInfo.processInfo.systemUptime-start)*1000)
    }
}

// Transport failures are HTTP errors; decoded application commands retain
// their durable receipt and application-level success/failure semantics.
@MainActor enum LANCommandResponse {
    static func make(_ data:Data,dispatch:(Command) async -> Receipt) async -> (String,Data) {
        guard let command=try? JSONCodec.decoder().decode(Command.self,from:data) else {
            return ("400 Bad Request",error("Invalid command"))
        }
        let receipt=await dispatch(command)
        do { return ("200 OK",try JSONCodec.encoder().encode(receipt)) }
        catch { return ("500 Internal Server Error",self.error("Cannot encode command receipt")) }
    }
    static func error(_ message:String)->Data {
        (try? JSONSerialization.data(withJSONObject:["status":"failed","result":message])) ?? Data("{}".utf8)
    }
}

// Direct local application control; no GitHub dependency.
final class LANServer {
    // Each accepted socket owns its buffer and timers so a slow or vanished
    // client cannot retain a control slot indefinitely or race a late reply.
    private final class Pending {
        let connection:NWConnection
        let id:ObjectIdentifier
        var buffer=Data()
        var responded=false
        var closed=false
        var phase:DispatchWorkItem?
        var hard:DispatchWorkItem?
        init(_ connection:NWConnection){self.connection=connection;id=ObjectIdentifier(connection)}
    }
    private var listener:NWListener?
    private var desiredRunning=false
    private var retry:DispatchWorkItem?
    private var retryGeneration=UUID()
    private var retryDelay:TimeInterval=1
    private let queue=DispatchQueue(label:"jealousfin.lan")
    private let listenPort:NWEndpoint.Port
    private let advertise:Bool
    private let maxConnections:Int
    private let idleTimeout:TimeInterval
    private let responseTimeout:TimeInterval
    private let queryTimeout:TimeInterval
    private let tokenLock=NSLock()
    private var accessToken:String?
    // Never queue.sync from the MainActor: the serial listener queue may be
    // parsing a large request body.
    func setAccessToken(_ token:String?){tokenLock.lock();accessToken=token;tokenLock.unlock()}
    private func token()->String?{tokenLock.lock();defer{tokenLock.unlock()};return accessToken}
    private var connections:[ObjectIdentifier:Pending]=[:]
    var handler:((Data,@escaping(String,Data)->Void)->Void)?
    var queryHandler:((String,@escaping(String,Data)->Void)->Void)?
    var state:((String)->Void)?
    init(port:NWEndpoint.Port=8765,advertise:Bool=true,maxConnections:Int=16,idleTimeout:TimeInterval=10,responseTimeout:TimeInterval=180,queryTimeout:TimeInterval=10){
        listenPort=port;self.advertise=advertise
        self.maxConnections=max(1,maxConnections)
        self.idleTimeout=max(0.05,idleTimeout)
        self.responseTimeout=max(0.05,responseTimeout)
        self.queryTimeout=max(0.05,queryTimeout)
    }
    var port:UInt16? {queue.sync{guard let value=listener?.port?.rawValue,value != 0 else{return nil};return value}}
    func start() throws {
        try queue.sync {
            desiredRunning=true
            guard listener==nil,retry==nil else{return}
            do{try startLocked()}catch{scheduleRetry(replaceWaiting:false);throw error}
        }
    }
    private func startLocked() throws {
            let parameters=NWParameters.tcp
            parameters.prohibitedInterfaceTypes=[.cellular]
            let l=try NWListener(using:parameters,on:listenPort)
            if advertise {l.service=NWListener.Service(name:nil,type:"_jealousfin._tcp")}
            l.stateUpdateHandler={ [weak self,weak l] status in
                guard let self=self,let l=l,self.listener===l else{return}
                self.state?(String(describing:status))
                switch status {
                case .ready:
                    self.cancelRetry()
                    self.retryDelay=1
                case .waiting:
                    self.scheduleRetry(replaceWaiting:true)
                case .failed,.cancelled:
                    self.cancelRetry()
                    self.invalidateListener()
                    self.scheduleRetry(replaceWaiting:false)
                default:break
                }
            }
            l.newConnectionHandler={ [weak self,weak l] c in
                guard let self=self,let l=l,self.listener===l,self.desiredRunning,self.connections.count<self.maxConnections else{c.cancel();return}
                let p=Pending(c)
                self.connections[p.id]=p
                c.stateUpdateHandler={ [weak self,weak p] status in
                    switch status {
                    case .failed,.cancelled:
                        if let p=p{self?.finish(p)}
                    default:break
                    }
                }
                c.start(queue:self.queue)
                self.armHard(p)
                self.armIdle(p)
                self.receive(p)
            }
            listener=l;l.start(queue:queue)
    }
    private func cancelRetry(){
        retryGeneration=UUID()
        retry?.cancel();retry=nil
    }
    private func invalidateListener(){
        let old=listener;listener=nil;old?.cancel()
        let pending=Array(connections.values)
        connections.removeAll()
        for p in pending{close(p)}
    }
    private func scheduleRetry(replaceWaiting:Bool){
        guard desiredRunning,retry==nil else{return}
        let generation=UUID();retryGeneration=generation
        let delay=replaceWaiting ? 15 : retryDelay
        if !replaceWaiting{retryDelay=min(15,retryDelay*2)}
        let item=DispatchWorkItem{[weak self] in
            guard let self=self,self.desiredRunning,self.retryGeneration==generation else{return}
            self.retry=nil
            if replaceWaiting{self.invalidateListener()}
            guard self.listener==nil else{return}
            do{try self.startLocked()}catch{
                self.state?("failed: listener restart")
                self.scheduleRetry(replaceWaiting:false)
            }
        }
        retry=item
        queue.asyncAfter(deadline:.now()+delay,execute:item)
    }
    func stop(){
        queue.sync{
            desiredRunning=false
            cancelRetry()
            invalidateListener()
            retryDelay=1
        }
    }
    private func isCurrent(_ p:Pending)->Bool{connections[p.id] === p}
    private func cancelPhase(_ p:Pending){p.phase?.cancel();p.phase=nil}
    private func armPhase(_ p:Pending,seconds:TimeInterval){
        cancelPhase(p)
        let item=DispatchWorkItem{[weak self,weak p] in
            guard let self=self,let p=p,self.isCurrent(p) else{return}
            self.finish(p)
        }
        p.phase=item
        queue.asyncAfter(deadline:.now()+seconds,execute:item)
    }
    private func armIdle(_ p:Pending){armPhase(p,seconds:idleTimeout)}
    private func armHard(_ p:Pending){
        p.hard?.cancel()
        let item=DispatchWorkItem{[weak self,weak p] in
            guard let self=self,let p=p else{return}
            self.finish(p)
        }
        p.hard=item
        queue.asyncAfter(deadline:.now()+max(responseTimeout,max(queryTimeout,idleTimeout)),execute:item)
    }
    private func close(_ p:Pending){
        p.closed=true
        cancelPhase(p)
        p.hard?.cancel();p.hard=nil
        p.buffer.removeAll(keepingCapacity:false)
        p.connection.cancel()
    }
    private func finish(_ p:Pending){
        guard isCurrent(p) else{return}
        connections.removeValue(forKey:p.id)
        close(p)
    }
    private func respond(_ p:Pending,_ status:String,_ data:Data){
        queue.async{[weak self,weak p] in
            guard let self=self,let p=p,!p.closed,!p.responded,self.isCurrent(p) else{return}
            p.responded=true
            // A client that never reads the response cannot retain the slot.
            self.armPhase(p,seconds:self.responseTimeout)
            let head="HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(data.count)\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
            var packet=Data(head.utf8);packet.append(data)
            p.connection.send(content:packet,completion:.contentProcessed{[weak self,weak p] _ in
                self?.queue.async{if let p=p{self?.finish(p)}}
            })
        }
    }
    private func errorBody(_ message:String)->Data{
        (try? JSONSerialization.data(withJSONObject:["status":"failed","result":message])) ?? Data("{}".utf8)
    }
    // Once the header is complete, reject unauthenticated callers before their
    // body is buffered. The final parse still performs the authoritative check.
    private nonisolated static func headerRequest(_ buffer:Data)->(method:String,path:String)?{
        guard let end=buffer.range(of:Data("\r\n\r\n".utf8)),
              let header=String(data:buffer[..<end.lowerBound],encoding:.utf8) else{return nil}
        let first=(header.components(separatedBy:"\r\n").first ?? "").split(separator:" ",omittingEmptySubsequences:false)
        guard first.count==3,first[1].hasPrefix("/") else{return nil}
        return (String(first[0]),String(first[1]))
    }
    private func receive(_ p:Pending){
        p.connection.receive(minimumIncompleteLength:1,maximumLength:65536){[weak self,weak p] data,_,done,error in
            guard let self=self,let p=p,self.isCurrent(p) else{p?.connection.cancel();return}
            guard error==nil else{self.finish(p);return}
            if let data=data{p.buffer.append(data)}
            if let header=Self.headerRequest(p.buffer),
               !(header.method=="GET" && header.path=="/health"),
               !LANHTTP.authorized(p.buffer,token:self.token()) {
                self.respond(p,"401 Unauthorized",self.errorBody("LAN authentication required"));return
            }
            switch LANHTTP.parse(p.buffer){
            case .incomplete:
                if done{self.finish(p)}else{self.armIdle(p);self.receive(p)}
            case .invalid(let status):self.cancelPhase(p);self.respond(p,status,self.errorBody(status))
            case .request(let method,let path,let body):
                self.cancelPhase(p)
                guard (method=="GET" && path=="/health") || LANHTTP.authorized(p.buffer,token:self.token()) else{
                    self.respond(p,"401 Unauthorized",self.errorBody("LAN authentication required"));return
                }
                if method=="POST" && path=="/command"{
                    guard let handler=self.handler else{self.respond(p,"503 Service Unavailable",self.errorBody("Command dispatcher unavailable"));return}
                    self.armPhase(p,seconds:self.responseTimeout)
                    handler(body){[weak self,weak p] status,output in
                        guard let self=self,let p=p else{return}
                        self.respond(p,status,output)
                    }
                }else if method=="GET"{
                    if path=="/health" && self.queryHandler==nil{
                        self.respond(p,"200 OK",Data("{\"service\":\"Jealousfin\",\"status\":\"reachable\"}".utf8));return
                    }
                    guard let handler=self.queryHandler else{self.respond(p,"404 Not Found",self.errorBody("Unknown route"));return}
                    self.armPhase(p,seconds:self.queryTimeout)
                    handler(path){[weak self,weak p] status,output in
                        guard let self=self,let p=p else{return}
                        self.respond(p,status,output)
                    }
                }else{self.respond(p,"404 Not Found",self.errorBody("Unknown route"))}
            }
        }
    }
}
