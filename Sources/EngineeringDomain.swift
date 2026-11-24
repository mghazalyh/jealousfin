import Foundation

struct RuntimePolicy: Codable, Equatable {
    var startupTimeout: Int
    var retries: Int
    var qualityMbps: Int
    var stallSeconds: Int
    var rotationSeconds: Int
    var validationSeconds: Int
    var minimumFrames: Int
    var forwardBufferSeconds: Int
    var automaticallyWaits: Bool
    var recoveryMode: String
    var failureRetrySeconds: Int
    static func baseline(_ settings: Settings) -> RuntimePolicy {
        RuntimePolicy(startupTimeout: settings.startupTimeout, retries: settings.retries,
            qualityMbps: settings.qualityMbps, stallSeconds: 30, rotationSeconds: settings.rotationMinutes * 60,
            validationSeconds: 3, minimumFrames: 3, forwardBufferSeconds: 0, automaticallyWaits: true, recoveryMode:"reopen", failureRetrySeconds:30)
    }
    func checked() throws -> RuntimePolicy {
        guard (5...120).contains(startupTimeout), (1...5).contains(retries), (1...50).contains(qualityMbps),
            (5...300).contains(stallSeconds), (5...86400).contains(rotationSeconds),
            (3...60).contains(validationSeconds), (3...120).contains(minimumFrames),
            (0...120).contains(forwardBufferSeconds), startupTimeout > validationSeconds,
            ["reopen","advance_surf","pause"].contains(recoveryMode),(5...600).contains(failureRetrySeconds)
        else { throw AppError.message("Runtime policy is outside supported native limits") }
        return self
    }
}
struct RuntimePatch: Codable, Equatable {
    var id: String
    var source: String
    var installedAt = Date()
}
struct RuntimePatchStore: Codable {
    var active: RuntimePatch?
    var previous: RuntimePatch?
    var activationPending = false
}
struct DeviceTestStep: Codable, Equatable {
    var kind: String
    var channelID: String?
    var seconds: Int?
    var source: String?
}
struct DeviceTestPlan: Codable, Equatable {
    var id: String
    var steps: [DeviceTestStep]
    var repeatCount: Int = 1
    var repeatForever: Bool = false
    func checked() throws -> DeviceTestPlan {
        guard id.range(of:"^[A-Za-z0-9_-]{1,80}$",options:.regularExpression) != nil,
              (1...200).contains(steps.count), (1...10000).contains(repeatCount)
        else { throw AppError.message("Invalid test plan ID, step count or repeats") }
        for s in steps {
            guard ["system","catalogue","playback","pause_resume","endurance","surf_rotation","background","stop","network","audio_route","reconnect","script_probe"].contains(s.kind),
                  (1...3600).contains(s.seconds ?? 30) else {throw AppError.message("Invalid test step")}
            if s.kind=="script_probe" {
                guard let source=s.source,!source.isEmpty,source.utf8.count<=24000 else{throw AppError.message("Invalid script probe")}
            }
        }
        return self
    }
}
struct DeviceTestResult: Codable, Identifiable {
    var id = UUID().uuidString
    var planID: String
    var cycle: Int
    var stepIndex: Int
    var kind: String
    var channelID: String?
    var startedAt: Date
    var finishedAt = Date()
    var status: String
    var evidence: [String:String]
    var launchID: String
    var patchID: String
}
struct DeviceTestCheckpoint: Codable {
    var plan: DeviceTestPlan
    var runID = UUID().uuidString
    var cycle = 0
    var stepIndex = 0
    var state = "queued"
    var updatedAt = Date()
    var lastResult: DeviceTestResult?
    var resultID:String {"\(runID)-\(cycle)-\(stepIndex)"}
    var canResume:Bool {["queued","running"].contains(state) && !plan.steps.isEmpty && stepIndex>=0 && stepIndex<plan.steps.count && cycle>=0}
    mutating func advance(_ result: DeviceTestResult) {
        guard result.planID==plan.id,result.cycle==cycle,result.stepIndex==stepIndex else{return}
        lastResult=result
        stepIndex+=1
        if stepIndex>=plan.steps.count {
            cycle+=1;stepIndex=0
            if !plan.repeatForever && cycle>=plan.repeatCount {state="completed"}
        }
        updatedAt=Date()
    }
}

struct ModuleAction: Codable {
    var action: String
    var arguments: [String:String]?
}
struct ModuleDecision: Codable {
    var state: [String:String]?
    var actions: [ModuleAction]?
    var nextSeconds: Int?
    var insights: [Insight]?
    func checked() throws -> ModuleDecision {
        guard (state?.count ?? 0)<=128,
            (state ?? [:]).allSatisfy({$0.key.utf8.count<=128 && $0.value.utf8.count<=4096}),
            (actions?.count ?? 0)<=8,
            (5...3600).contains(nextSeconds ?? 15),
            (insights?.count ?? 0)<=20,
            (insights ?? []).allSatisfy({["Emergency","High","Attention","Informational","Resolved"].contains($0.priority) && $0.id.count<=128 && $0.title.count<=512 && $0.explanation.count<=4096 && $0.action.count<=2048})
        else {throw AppError.message("Module decision exceeds supported bounds")}
        return self
    }
}
struct ModuleCheckpoint: Codable {
    var moduleID:String
    var state:[String:String] = [:]
    var nextAt=Date()
    var insights:[Insight] = []
    var lastReceipt:[String] = []
    var eventCursor:String?
    var pending:[Command]?
}
