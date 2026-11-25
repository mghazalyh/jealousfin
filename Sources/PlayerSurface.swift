import SwiftUI
import AVKit
import AVFoundation

struct PlayerSurface: UIViewControllerRepresentable {
    var player: AVPlayer
    var onReady: (Bool)->Void
    var showsControls:Bool = true
    var gravity:String = "fit"
    var allowsPiP:Bool = true
    var automaticPiP:Bool = false
    var brightness:Int = 0
    private var videoGravity:AVLayerVideoGravity {
        switch gravity{case "fill":return .resizeAspectFill;case "stretch":return .resize;default:return .resizeAspect}
    }
    func makeCoordinator()->Coordinator { Coordinator(onReady) }
    final class Coordinator {
        var callback:(Bool)->Void
        var observation:NSKeyValueObservation?
        var boundsObservation:NSKeyValueObservation?
        let brightnessLayer=UIView()
        var level=0
        @MainActor func applyBrightness(_ vc:AVPlayerViewController){
            guard let overlay=vc.contentOverlayView else{return}
            if brightnessLayer.superview !== overlay{
                brightnessLayer.removeFromSuperview()
                brightnessLayer.isUserInteractionEnabled=false
                brightnessLayer.isAccessibilityElement=false
                overlay.addSubview(brightnessLayer)
            }
            let rect=vc.view.convert(vc.videoBounds,to:overlay).intersection(overlay.bounds)
            brightnessLayer.frame=rect.isNull ? .zero:rect
            brightnessLayer.backgroundColor=level<0 ? .black:.white
            brightnessLayer.alpha=CGFloat(DynamicPresentation.videoBrightnessOpacity(level))
            brightnessLayer.isHidden=level==0 || !vc.isReadyForDisplay || rect.isEmpty
        }
        init(_ callback:@escaping(Bool)->Void){self.callback=callback}
    }
    func makeUIViewController(context:Context)->AVPlayerViewController {
        let c=AVPlayerViewController()
        c.player=player
        c.showsPlaybackControls=showsControls
        c.videoGravity=videoGravity
        c.allowsPictureInPicturePlayback=allowsPiP
        c.canStartPictureInPictureAutomaticallyFromInline=allowsPiP && automaticPiP
        context.coordinator.observation=c.observe(\.isReadyForDisplay,options:[.initial,.new]){ [weak coordinator=context.coordinator] vc,_ in
            let ready=vc.isReadyForDisplay
            DispatchQueue.main.async { coordinator?.callback(ready);coordinator?.applyBrightness(vc) }
        }
        context.coordinator.level=min(20,max(-25,brightness))
        context.coordinator.boundsObservation=c.observe(\.videoBounds,options:[.new]){[weak coordinator=context.coordinator] vc,_ in
            DispatchQueue.main.async{coordinator?.applyBrightness(vc)}
        }
        context.coordinator.applyBrightness(c)
        return c
    }
    func updateUIViewController(_ uiViewController:AVPlayerViewController,context:Context) {
        context.coordinator.callback=onReady
        if uiViewController.player !== player {uiViewController.player=player}
        uiViewController.showsPlaybackControls=showsControls
        uiViewController.videoGravity=videoGravity
        uiViewController.allowsPictureInPicturePlayback=allowsPiP
        uiViewController.canStartPictureInPictureAutomaticallyFromInline=allowsPiP && automaticPiP
        context.coordinator.level=min(20,max(-25,brightness))
        context.coordinator.applyBrightness(uiViewController)
    }
    static func dismantleUIViewController(_ uiViewController:AVPlayerViewController,coordinator:Coordinator){
        coordinator.observation?.invalidate()
        coordinator.observation=nil
        coordinator.boundsObservation?.invalidate()
        coordinator.boundsObservation=nil
        coordinator.brightnessLayer.removeFromSuperview()
        // Do not pause the shared AVPlayer when switching surfaces.
        uiViewController.player=nil
    }
}
