import Foundation

struct DecisionChoice:Codable,Equatable,Identifiable {
    var id:String
    var title:String
    var detail:String?
    var action:DynamicUIAction?
}
struct DecisionPrompt:Codable,Equatable,Identifiable {
    var schemaVersion:Int
    var id:String
    var title:String
    var message:String
    var priority:String
    var evidence:[String:String]?
    var expiresAt:Date
    var expectedChannelID:String?
    var expectedPatchID:String?
    var expectedJellyfinAPIRevision:String?
    var choices:[DecisionChoice]
    static let repairCommands:Set<String>=DynamicUILayout.commands.subtracting(["script"]).union(["install_runtime_module_from_db","rollback_runtime_module","reload_runtime_module","install_ui_layout_from_db","rollback_ui_layout","reset_runtime_module","reset_ui_layout","install_jellyfin_api_from_db","rollback_jellyfin_api","reset_jellyfin_api"])
    func checked(now:Date)throws->DecisionPrompt{
        guard schemaVersion==1,DynamicUILayout.validID(id),!title.isEmpty,title.count<=160,
              !message.isEmpty,message.count<=4000,["Emergency","High","Attention","Informational"].contains(priority),
              expiresAt>now,expiresAt.timeIntervalSince(now)<=7*86400,
              (2...4).contains(choices.count),Set(choices.map(\.id)).count==choices.count,
              choices.contains(where:{$0.action==nil}),(evidence?.count ?? 0)<=12,
              (evidence ?? [:]).allSatisfy({$0.key.count<=100 && $0.value.count<=1000}),
              (expectedChannelID?.count ?? 0)<=100,(expectedPatchID?.count ?? 0)<=100,(expectedJellyfinAPIRevision?.count ?? 0)<=100
        else{throw AppError.message("Invalid or expired decision prompt")}
        for choice in choices {
            guard DynamicUILayout.validID(choice.id),!choice.title.isEmpty,choice.title.count<=100,
                  (choice.detail?.count ?? 0)<=1000 else{throw AppError.message("Invalid decision choice")}
            if let action=choice.action{
                guard Self.repairCommands.contains(action.command),(action.arguments?.count ?? 0)<=24,
                    (action.arguments ?? [:]).allSatisfy({$0.key.count<=128 && $0.value.utf8.count<=8192 && !$0.value.hasPrefix("$state.") && !$0.value.hasPrefix("$form.")})
                else{throw AppError.message("Decision action must be a registered command with fixed arguments")}
            }
        }
        return self
    }
    func contextMatches(channelID:String?,patchID:String,apiRevision:String?=nil)->Bool{
        (expectedChannelID.map{$0==channelID} ?? true) && (expectedPatchID.map{$0==patchID} ?? true) && (expectedJellyfinAPIRevision.map{$0==apiRevision} ?? true)
    }
}
struct DecisionRecord:Codable,Identifiable {
    var prompt:DecisionPrompt
    var receivedAt=Date()
    var presentedAt:Date?
    var answeredAt:Date?
    var completedAt:Date?
    var selectedChoiceID:String?
    var status="pending"
    var receipt:Receipt?
    var commandID="decision-"+UUID().uuidString
    var revision=1
    var id:String{prompt.id}
    var terminal:Bool{["declined","expired","cancelled","stale","succeeded","failed","interrupted"].contains(status)}
    mutating func present(at now:Date){
        guard status=="pending",presentedAt==nil,prompt.expiresAt>now else{return}
        presentedAt=now;revision+=1
    }
    mutating func expire(at now:Date){
        guard status=="pending",prompt.expiresAt<=now else{return}
        status="expired";completedAt=now;revision+=1
    }
    mutating func cancel(at now:Date)throws{
        guard status=="pending" else{throw AppError.message("Decision is no longer pending")}
        status="cancelled";completedAt=now;revision+=1
    }
    mutating func choose(_ id:String,at now:Date)throws{
        guard status=="pending",prompt.expiresAt>now,presentedAt != nil,
              let choice=prompt.choices.first(where:{$0.id==id}) else{throw AppError.message("Decision is unavailable or expired")}
        selectedChoiceID=id;answeredAt=now
        if choice.action==nil{status="declined";completedAt=now}
        else{status="approved"}
        revision+=1
    }
    var chosenAction:DynamicUIAction?{prompt.choices.first(where:{$0.id==selectedChoiceID})?.action}
    mutating func finish(_ receipt:Receipt,at now:Date){
        guard status=="approved",receipt.id==commandID,receipt.status != "running" else{return}
        self.receipt=receipt;status=receipt.status=="succeeded" ? "succeeded":receipt.status=="interrupted" ? "interrupted":"failed"
        completedAt=now;revision+=1
    }
    mutating func stale(at now:Date){
        guard status=="approved" else{return}
        status="stale";completedAt=now;revision+=1
    }
}
struct DecisionJournal:Codable {
    var records:[DecisionRecord]=[]
    var uploaded:[String:Int]=[:]
    func next(at now:Date)->DecisionRecord?{
        let order=["Emergency":0,"High":1,"Attention":2,"Informational":3]
        return records.filter{$0.status=="pending" && $0.prompt.expiresAt>now}.sorted{
            let a=order[$0.prompt.priority] ?? 4,b=order[$1.prompt.priority] ?? 4
            return a==b ? $0.receivedAt<$1.receivedAt:a<b
        }.first
    }
    mutating func insert(_ prompt:DecisionPrompt,at now:Date)throws{
        if let old=records.first(where:{$0.id==prompt.id}){
            guard old.prompt==prompt else{throw AppError.message("Decision ID already used")}
            return
        }
        _ = try prompt.checked(now:now)
        guard records.filter({!$0.terminal}).count<20 else{throw AppError.message("Decision inbox full")}
        records.append(DecisionRecord(prompt:prompt,receivedAt:now))
    }
    mutating func markUploaded(id:String,revision:Int){
        uploaded[id]=max(uploaded[id] ?? 0,revision)
    }
    var pendingUpload:[DecisionRecord]{
        records.filter{(uploaded[$0.id] ?? 0)<$0.revision}
    }
}
