import SwiftUI

// Rendering stays native on iOS 15+. A layout only describes compositions of
// these stable primitives, so a failed revision never replaces the native shell.
struct DynamicUISlotView: View {
    @EnvironmentObject var model:AppModel
    let location:String
    var body:some View {
        if let layout=model.dynamicUI.active,let slot=layout.slots.first(where:{$0.location==location}),!slot.nodes.isEmpty {
            DynamicUINodes(nodes:slot.nodes)
                .id(layout.revision+":"+location)
                .onAppear{model.observeDynamicSlot(location)}
        }
    }
}
struct DynamicUINodes:View{
    let nodes:[DynamicUINode]
    var body:some View{
        ForEach(nodes){node in DynamicUINodeView(node:node)}
    }
}
struct DynamicUIPageView:View{
    @EnvironmentObject var model:AppModel
    let page:DynamicUIPage
    var body:some View{
        NavigationView{
            Screen{DynamicUINodes(nodes:page.nodes)}
                .navigationTitle(page.title).navigationBarTitleDisplayMode(.inline)
                .toolbar{ToolbarItem(placement:.cancellationAction){Button("Close"){model.dynamicPage=nil}}}
                .onAppear{model.observeDynamicSlot("page:"+page.id)}
        }.navigationViewStyle(.stack)
        .overlay{DecisionOverlay().environmentObject(model)}
    }
}
struct DynamicUINodeView:View{
    @EnvironmentObject var model:AppModel
    let node:DynamicUINode
    private var title:String{node.title ?? ""}
    private var value:String{model.dynamicValue(node.valueKey) ?? node.defaultValue ?? ""}
    private var lower:Double{node.minimum ?? 0}
    private var upper:Double{node.maximum ?? 100}
    private var number:Double{
        let parsed=Double(value) ?? lower
        return parsed.isFinite ? min(upper,max(lower,parsed)):lower
    }
    private var form:Binding<String>{
        Binding(get:{value},set:{if let key=node.valueKey{model.setDynamicUIValueFromView(key,value:$0)}})
    }
    private var font:Font {
        switch node.style{
        case "headline":return .headline
        case "caption":return .caption
        default:return .body
        }
    }
    private var foreground:Color{
        switch node.style{
        case "muted","caption":return .secondary
        case "destructive":return .red
        default:return .primary
        }
    }
    var body:some View{
        if model.dynamicVisible(node){
            content.disabled(!model.dynamicEnabled(node))
                .accessibilityIdentifier("dynamic-ui."+node.id)
        }
    }
    // Type erasure breaks the recursive view type for nested dynamic containers.
    private var content:AnyView{
        switch node.kind{
        case "text":
            return AnyView(VStack(alignment:.leading,spacing:4){
                label.font(font).foregroundColor(foreground)
                if !value.isEmpty{Text(value).font(.subheadline).textSelection(.enabled)}
                if let detail=node.detail{Text(detail).font(.caption).foregroundColor(.secondary)}
            }.frame(maxWidth:.infinity,alignment:.leading))
        case "metric":
            return AnyView(Panel{VStack(alignment:.leading,spacing:5){
                label.font(.caption).foregroundColor(.secondary)
                Text(value.isEmpty ? "Unavailable":value).font(.title2.monospacedDigit()).textSelection(.enabled)
                if let detail=node.detail{Text(detail).font(.caption).foregroundColor(.secondary)}
            }})
        case "button":
            return AnyView(VStack(alignment:.leading,spacing:6){
                if node.style=="prominent"{
                    actionButton.buttonStyle(.borderedProminent)
                }else{actionButton.buttonStyle(.bordered).tint(node.style=="destructive" ? .red:nil)}
                if let detail=node.detail{Text(detail).font(.caption).foregroundColor(.secondary)}
                if let result=model.dynamicUIResults[node.id]{
                    Text(result).font(.system(.caption,design:.monospaced)).textSelection(.enabled).lineLimit(12)
                }
            })
        case "input":
            return AnyView(VStack(alignment:.leading,spacing:5){
                if !title.isEmpty{Text(title).font(.caption).foregroundColor(.secondary)}
                TextField(node.detail ?? title,text:form).textFieldStyle(.roundedBorder).autocapitalization(.none)
            })
        case "toggle":
            return AnyView(Toggle(title,isOn:Binding(get:{DynamicUIBinding.truth(value)},set:{form.wrappedValue=String($0)})))
        case "picker":
            return AnyView(Picker(title,selection:form){
                ForEach(node.options ?? []){option in Text(option.title).tag(option.value)}
            })
        case "slider":
            return AnyView(VStack(alignment:.leading,spacing:6){
                HStack{Text(title);Spacer();Text(String(format:"%.2f",number)).monospacedDigit()}
                Slider(value:Binding(get:{number},set:{form.wrappedValue=String($0)}),in:lower...upper,step:node.step ?? 1)
            })
        case "progress":
            return AnyView(VStack(alignment:.leading,spacing:6){
                Text(title).font(.caption)
                if Double(value)?.isFinite==true {
                    ProgressView(value:number-lower,total:upper-lower)
                }else{Text("Unavailable").font(.caption).foregroundColor(.secondary)}
            })
        case "card":
            return AnyView(Panel{VStack(alignment:.leading,spacing:10){
                if !title.isEmpty{label.font(.headline)}
                if let detail=node.detail{Text(detail).font(.caption).foregroundColor(.secondary)}
                DynamicUINodes(nodes:node.children ?? [])
            }})
        case "vstack":
            return AnyView(VStack(alignment:.leading,spacing:10){DynamicUINodes(nodes:node.children ?? [])})
        case "hstack":
            return AnyView(HStack(alignment:.center,spacing:8){DynamicUINodes(nodes:node.children ?? [])})
        case "web":
            return AnyView(DynamicWebComponent(node:node,revision:model.dynamicUI.active?.revision ?? "").frame(height:CGFloat(node.height ?? 200)))
        case "divider":return AnyView(Divider())
        case "spacer":return AnyView(Spacer(minLength:8))
        case "link":
            return AnyView(Button{if let page=node.targetPage{model.openDynamicPageFromView(page)}}label:{
                HStack{label;Spacer();Image(systemName:"chevron.right").font(.caption)}
            }.buttonStyle(.bordered))
        default:return AnyView(EmptyView())
        }
    }
    @ViewBuilder private var label:some View{
        if let symbol=node.symbol,!symbol.isEmpty{Label(title,systemImage:symbol)}
        else{Text(title)}
    }
    private var actionButton:some View{
        Button{model.triggerDynamicUIFromView(node.id)}label:{
            HStack{if model.dynamicUIBusy.contains(node.id){ProgressView()};label}
                .frame(maxWidth:.infinity)
        }.disabled(model.dynamicUIBusy.contains(node.id))
    }
}
