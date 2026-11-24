import SwiftUI

struct PatchVersionHistoryView:View{
    @EnvironmentObject var model:AppModel
    @State private var label=""
    @State private var scope=PatchVersionScope.all
    @State private var selected:PatchVersionSummary?
    @State private var result=""
    @State private var busy=false
    @State private var knownGood=false
    @State private var shown=30
    var body:some View{
        Screen{
            Text("Hot patch versions").font(.largeTitle.bold())
            Panel{VStack(alignment:.leading,spacing:10){
                Text("Current head").font(.headline)
                Text(model.patchArchiveID ?? "Not yet archived").font(.caption.monospaced()).textSelection(.enabled)
                TextField("Checkpoint name",text:$label).textFieldStyle(.roundedBorder)
                Toggle("Mark tested and working",isOn:$knownGood)
                Button("Save checkpoint"){checkpoint()}.buttonStyle(.borderedProminent)
                    .disabled(label.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty || busy || model.versionBusy)
                Picker("Restore scope",selection:$scope){
                    Text("All components").tag(PatchVersionScope.all)
                    Text("Runtime only").tag(PatchVersionScope.runtime)
                    Text("Jellyfin API only").tag(PatchVersionScope.jellyfinAPI)
                    Text("UI only").tag(PatchVersionScope.ui)
                }
                if !result.isEmpty{Text(result).font(.caption).textSelection(.enabled)}
            }}
            ForEach(Array(model.patchVersions.prefix(shown))){version in
                Panel{VStack(alignment:.leading,spacing:6){
                    HStack{
                        Text(version.label ?? version.reason).font(.headline)
                        Spacer()
                        if version.id==model.patchArchiveID{Text("Current").font(.caption).foregroundColor(.green)}
                        else if version.knownGood{Text("Known good").font(.caption).foregroundColor(.green)}
                    }
                    Text(version.recordedAt.formatted(date:.abbreviated,time:.standard)).font(.caption)
                    Text("Runtime: "+version.runtime+"\nAPI: "+version.jellyfinAPI+"\nUI: "+version.ui).font(.caption.monospaced())
                    Text(version.id).font(.caption2.monospaced()).foregroundColor(.secondary).textSelection(.enabled)
                    if let parent=version.parentID{Text("Parent: "+parent).font(.caption2).foregroundColor(.secondary)}
                    if let target=version.restoredFrom{Text("Restored from: "+target).font(.caption2).foregroundColor(.secondary)}
                    Button("Restore this version"){selected=version}.buttonStyle(.bordered)
                        .disabled(busy || model.versionBusy || version.id==model.patchArchiveID)
                }}
            }
            if shown<model.patchVersions.count{Button("Show more"){shown+=30}}
        }.navigationTitle("Version history").navigationBarTitleDisplayMode(.inline)
            .confirmationDialog("Restore selected patch version?",isPresented:Binding(get:{selected != nil},set:{if !$0{selected=nil}}),titleVisibility:.visible,presenting:selected){version in
                Button("Restore "+scope.rawValue){restore(version)}
                Button("Cancel",role:.cancel){selected=nil}
            }message:{version in Text("Restore "+(version.label ?? version.id)+"? Current source is retained as a new recoverable checkpoint. Playback may reconnect.")}
    }
    private func checkpoint(){
        busy=true
        Task{
            let receipt=await model.execute(Command(id:UUID().uuidString,action:"checkpoint_patch_version",arguments:["label":label,"knownGood":String(knownGood)],expiresAt:nil))
            result=receipt.result
            if receipt.status=="succeeded"{label="";knownGood=false}
            busy=false
        }
    }
    private func restore(_ version:PatchVersionSummary){
        guard let expected=model.patchArchiveID else{return}
        let chosenScope=scope.rawValue
        selected=nil;busy=true
        Task{
            let receipt=await model.execute(Command(id:UUID().uuidString,action:"restore_patch_version",arguments:["id":version.id,"scope":chosenScope,"expectedHead":expected],expiresAt:nil))
            result=receipt.result;busy=false
        }
    }
}
