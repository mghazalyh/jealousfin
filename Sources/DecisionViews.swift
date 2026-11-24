import SwiftUI

// A native overlay avoids replacing the current playback view or opening a
// competing sheet. The same overlay is attached to the custom-page root.
struct DecisionOverlay:View {
    @EnvironmentObject var model:AppModel
    @Environment(\.scenePhase) private var phase
    var body:some View{
        if phase == .active,let record=model.activeDecision {
            DecisionCard(record:record).id(record.id)
        }
    }
}
private struct DecisionCard:View{
    @EnvironmentObject var model:AppModel
    let record:DecisionRecord
    @State private var issue=""
    private var priorityColor:Color{
        switch record.prompt.priority{
        case "Emergency":return .red
        case "High":return .orange
        default:return Color(red:0.69,green:0.52,blue:1)
        }
    }
    var body:some View{
        ZStack{
            Color.black.opacity(0.65).ignoresSafeArea()
            ScrollView{
                VStack(alignment:.leading,spacing:14){
                    Label(record.prompt.priority.uppercased(),systemImage:"exclamationmark.bubble")
                        .font(.caption.bold()).foregroundColor(priorityColor)
                    Text(record.prompt.title).font(.title2.bold())
                    Text(record.prompt.message).font(.subheadline)
                    if let evidence=record.prompt.evidence,!evidence.isEmpty{
                        Divider()
                        ForEach(evidence.keys.sorted(),id:\.self){key in
                            HStack(alignment:.top){
                                Text(key).foregroundColor(.secondary)
                                Spacer()
                                Text(evidence[key] ?? "").multilineTextAlignment(.trailing)
                            }.font(.caption)
                        }
                    }
                    Text("Expires "+record.prompt.expiresAt.formatted(date:.abbreviated,time:.shortened))
                        .font(.caption2).foregroundColor(.secondary)
                    ForEach(record.prompt.choices){choice in
                        VStack(alignment:.leading,spacing:5){
                            if let detail=choice.detail{Text(detail).font(.caption).foregroundColor(.secondary)}
                            if choice.action==nil{
                                Button(choice.title,role:.cancel){answer(choice.id)}
                                    .buttonStyle(.bordered).frame(maxWidth:.infinity)
                            }else{
                                Button{answer(choice.id)}label:{
                                    Text(choice.title).frame(maxWidth:.infinity)
                                }.buttonStyle(.borderedProminent)
                            }
                        }
                    }
                    if !issue.isEmpty{Text(issue).foregroundColor(.orange).font(.caption)}
                }.padding(20)
                    .frame(maxWidth:440,alignment:.leading)
                    .background(Color(red:0.095,green:0.103,blue:0.16))
                    .cornerRadius(20)
                    .shadow(radius:20)
                    .padding(24)
            }.frame(maxHeight:640)
        }
        .accessibilityIdentifier("decision."+record.id)
        .onAppear{model.decisionPresented(record.id)}
    }
    private func answer(_ id:String){
        do{try model.answerDecision(record.id,choice:id);issue=""}
        catch{issue=error.localizedDescription}
    }
}
struct DecisionHistoryView:View{
    @EnvironmentObject var model:AppModel
    var body:some View{
        let latest=Array(model.decisions.records.suffix(8).reversed())
        if !latest.isEmpty{
            SectionLabel(title:"Engineering decisions")
            ForEach(latest){record in
                Panel{
                    VStack(alignment:.leading,spacing:6){
                        Text(record.prompt.title).font(.headline)
                        Text(record.status=="approved" ? "Approved · applying":record.status.capitalized).font(.caption.bold())
                            .foregroundColor(record.status=="succeeded" ? .green:record.status=="failed" || record.status=="interrupted" ? .orange:.secondary)
                        if let choice=record.prompt.choices.first(where:{$0.id==record.selectedChoiceID}){
                            Text("Your choice: "+choice.title).font(.caption)
                        }
                        if let receipt=record.receipt{
                            Text(receipt.result).font(.caption).foregroundColor(.secondary).lineLimit(6)
                        }
                    }
                }
            }
        }
    }
}
