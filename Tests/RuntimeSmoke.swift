import Foundation

@main struct RuntimeSmoke {
    @MainActor static func main() async throws {
        let engine=ScriptEngine()
        let (value,commands)=try await engine.evaluateAsync("api.command('get_status', {}); api.state.answer + 1",snapshot:["answer":41])
        precondition(value=="42" && commands.count==1 && commands[0].action=="get_status")
        let start=ProcessInfo.processInfo.systemUptime
        var interrupted=false
        do{_ = try await engine.evaluateAsync("while (true) {}",snapshot:[:])}
        catch{interrupted=error.localizedDescription.lowercased().contains("deadline")}
        precondition(interrupted && ProcessInfo.processInfo.systemUptime-start<10)
        let (after,_)=try await engine.evaluateAsync("6*7",snapshot:[:])
        precondition(after=="42")
        var exception=false
        do{_ = try await engine.evaluateAsync("throw new Error('fixture')",snapshot:[:])}
        catch{exception=true}
        precondition(exception)
        print("PASS: runtime bridge, infinite-loop interrupt, subsequent evaluation and error recovery")
    }
}
