import SwiftUI
import WebKit

// Local HTML/CSS/JS fragments with the application-command bridge.
struct DynamicWebComponent:UIViewRepresentable{
    @EnvironmentObject var model:AppModel
    let node:DynamicUINode
    let revision:String

    func makeCoordinator()->Coordinator{Coordinator(model:model,nodeID:node.id,revision:revision)}
    func makeUIView(context:Context)->WKWebView{
        let config=WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(context.coordinator,name:"jealousfin")
        let view=WKWebView(frame:.zero,configuration:config)
        view.navigationDelegate=context.coordinator
        view.isOpaque=false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        context.coordinator.load(node.html ?? "",in:view)
        return view
    }
    func updateUIView(_ view:WKWebView,context:Context){
        context.coordinator.model=model
        context.coordinator.nodeID=node.id
        context.coordinator.revision=revision
        context.coordinator.load(node.html ?? "",in:view)
        context.coordinator.publish(in:view)
    }
    static func dismantleUIView(_ view:WKWebView,coordinator:Coordinator){
        view.stopLoading()
        view.configuration.userContentController.removeScriptMessageHandler(forName:"jealousfin")
        view.navigationDelegate=nil
    }
    @MainActor final class Coordinator:NSObject,WKScriptMessageHandler,WKNavigationDelegate{
        weak var model:AppModel?
        var nodeID:String
        var revision:String
        private var loaded:String?
        private var ready=false
        private var lastPublish=Date.distantPast
        init(model:AppModel,nodeID:String,revision:String){
            self.model=model;self.nodeID=nodeID;self.revision=revision
        }
        func load(_ fragment:String,in view:WKWebView){
            guard loaded != fragment else{return}
            loaded=fragment;ready=false
            let document="""
            <!doctype html><html><head>
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
            <style>:root{color-scheme:dark}body{margin:0;background:transparent;color:#f4f2fa;font:15px -apple-system,BlinkMacSystemFont,sans-serif}button,input,select{font:inherit}*{box-sizing:border-box}</style>
            <script>
            window.jealousfinState = {};
            window.jealousfin = {command:function(command,args,requestID){
                window.webkit.messageHandlers.jealousfin.postMessage({command:command,arguments:args||{},requestID:requestID||""});
            }};
            </script></head><body>
            \(fragment)
            </body></html>
            """
            view.loadHTMLString(document,baseURL:nil)
        }
        func publish(in view:WKWebView,force:Bool=false){
            guard ready,let model=model,force || Date().timeIntervalSince(lastPublish)>=1 else{return}
            lastPublish=Date()
            guard let data=try? JSONSerialization.data(withJSONObject:model.stateSnapshot(),options:[.sortedKeys]),
                let json=String(data:data,encoding:.utf8) else{return}
            view.evaluateJavaScript("window.jealousfinState=\(json);window.dispatchEvent(new CustomEvent('jealousfin-state',{detail:window.jealousfinState}));",completionHandler:nil)
        }
        func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!){
            ready=true;publish(in:webView,force:true)
            model?.observeDynamicSlot("web:"+nodeID)
        }
        func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
            let url=navigationAction.request.url
            decisionHandler(url?.scheme=="about" && url?.absoluteString=="about:blank" ? .allow:.cancel)
        }
        func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
            guard message.name=="jealousfin",message.frameInfo.isMainFrame,let model=model,
                let view=message.webView,let body=message.body as? [String:Any],
                let command=body["command"] as? String,
                let args=body["arguments"] as? [String:String] else{return}
            let requestID=String((body["requestID"] as? String ?? "").prefix(128))
            let node=nodeID,rev=revision
            Task{ @MainActor [weak self,weak view] in
                let receipt=await model.executeWebUI(nodeID:node,revision:rev,action:DynamicUIAction(command:command,arguments:args))
                guard let self=self,let view=view,self.revision==rev,self.nodeID==node else{return}
                let payload:[String:String]=["requestID":requestID,"receiptID":receipt.id,"status":receipt.status,"result":String(receipt.result.prefix(8192))]
                guard let data=try? JSONSerialization.data(withJSONObject:payload),let json=String(data:data,encoding:.utf8) else{return}
                view.evaluateJavaScript("window.dispatchEvent(new CustomEvent('jealousfin-receipt',{detail:\(json)}));",completionHandler:nil)
            }
        }
    }
}
