import Foundation
import Network

// Scripts execute application APIs, not an operating-system shell.
// JavaScriptCore is interpreted on the target device; native changes still require an IPA.
@MainActor final class ScriptEngine {}

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
    private var listener:NWListener?
    private let queue=DispatchQueue(label:"jealousfin.lan")
    private let listenPort:NWEndpoint.Port
    private let advertise:Bool
    private var connections:[ObjectIdentifier:NWConnection]=[:]
    var handler:((Data,@escaping(String,Data)->Void)->Void)?
    var queryHandler:((String,@escaping(String,Data)->Void)->Void)?
    var state:((String)->Void)?
    init(port:NWEndpoint.Port=8765,advertise:Bool=true){listenPort=port;self.advertise=advertise}
    var port:UInt16? {queue.sync{guard let value=listener?.port?.rawValue,value != 0 else{return nil};return value}}
    func start() throws {
        try queue.sync {
            guard listener==nil else{return}
            let parameters=NWParameters.tcp
            parameters.prohibitedInterfaceTypes=[.cellular]
            let l=try NWListener(using:parameters,on:listenPort)
            if advertise {l.service=NWListener.Service(name:nil,type:"_jealousfin._tcp")}
            l.stateUpdateHandler={ [weak self,weak l] status in
                guard let self=self,let l=l,self.listener===l else{return}
                self.state?(String(describing:status))
                if case .failed = status {self.listener=nil;l.cancel()}
            }
            l.newConnectionHandler={ [weak self] c in
                guard let self=self,self.connections.count<16 else{c.cancel();return}
                let id=ObjectIdentifier(c)
                self.connections[id]=c
                c.stateUpdateHandler={ [weak self,weak c] status in
                    switch status {
                    case .failed,.cancelled:
                        if let c=c{self?.finish(c)}
                    default:break
                    }
                }
                c.start(queue:self.queue)
                self.receive(c,buffer:Data())
                self.queue.asyncAfter(deadline:.now()+180){[weak self,weak c] in
                    if let c=c{self?.finish(c)}
                }
            }
            listener=l;l.start(queue:queue)
        }
    }
    func stop(){
        queue.sync{
            listener?.cancel();listener=nil
            for c in connections.values{c.cancel()}
            connections.removeAll()
        }
    }
    private func finish(_ c:NWConnection){
        if connections.removeValue(forKey:ObjectIdentifier(c)) != nil{c.cancel()}
    }
    private func respond(_ c:NWConnection,_ status:String,_ data:Data){
        queue.async{[weak self] in
            guard let self=self,self.connections[ObjectIdentifier(c)] != nil else{return}
            let head="HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(data.count)\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
            var packet=Data(head.utf8);packet.append(data)
            c.send(content:packet,completion:.contentProcessed{[weak self] _ in self?.queue.async{self?.finish(c)}})
        }
    }
    private func errorBody(_ message:String)->Data{
        (try? JSONSerialization.data(withJSONObject:["status":"failed","result":message])) ?? Data("{}".utf8)
    }
    private func receive(_ c:NWConnection,buffer:Data){
        c.receive(minimumIncompleteLength:1,maximumLength:65536){[weak self] data,_,done,error in
            guard let self=self,self.connections[ObjectIdentifier(c)] != nil else{c.cancel();return}
            guard error==nil else{self.finish(c);return}
            var b=buffer;if let data=data{b.append(data)}
            switch LANHTTP.parse(b){
            case .incomplete:
                if done{self.finish(c)}else{self.receive(c,buffer:b)}
            case .invalid(let status):self.respond(c,status,self.errorBody(status))
            case .request(let method,let path,let body):
                if method=="POST" && path=="/command"{
                    guard let handler=self.handler else{self.respond(c,"503 Service Unavailable",self.errorBody("Command dispatcher unavailable"));return}
                    handler(body){[weak self] status,output in self?.respond(c,status,output)}
                }else if method=="GET"{
                    if path=="/health" && self.queryHandler==nil{
                        self.respond(c,"200 OK",Data("{\"service\":\"Jealousfin\",\"status\":\"reachable\"}".utf8));return
                    }
                    guard let handler=self.queryHandler else{self.respond(c,"404 Not Found",self.errorBody("Unknown route"));return}
                    handler(path){[weak self] status,output in self?.respond(c,status,output)}
                }else{self.respond(c,"404 Not Found",self.errorBody("Unknown route"))}
            }
        }
    }
}
