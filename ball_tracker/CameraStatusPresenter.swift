import UIKit

final class CameraStatusPresenter {
    private struct StateVisualConfig {
        let borderColor: UIColor
        let borderWidth: CGFloat
        let pulse: Bool
        let chipText: String
        let chipStyle: StatusChip.Style
    }

    private weak var topStatusChip: StatusChip?
    private weak var warningLabel: UILabel?
    private weak var connectionLabel: UILabel?
    private weak var previewLabel: UILabel?
    private weak var stateBorderLayer: CAShapeLayer?

    private var lastRenderedState: CameraViewController.AppState?

    init(
        topStatusChip: StatusChip,
        warningLabel: UILabel,
        connectionLabel: UILabel,
        previewLabel: UILabel,
        stateBorderLayer: CAShapeLayer
    ) {
        self.topStatusChip = topStatusChip
        self.warningLabel = warningLabel
        self.connectionLabel = connectionLabel
        self.previewLabel = previewLabel
        self.stateBorderLayer = stateBorderLayer
    }

    func render(
        state: CameraViewController.AppState,
        connectionText: String,
        previewText: String,
        onRecordingChanged: (_ isRecording: Bool) -> Void
    ) {
        connectionLabel?.text = connectionText
        previewLabel?.text = previewText

        guard lastRenderedState != state else { return }
        lastRenderedState = state

        let cfg = stateVisualConfig(for: state)
        stateBorderLayer?.strokeColor = cfg.borderColor.cgColor
        stateBorderLayer?.lineWidth = cfg.borderWidth
        stateBorderLayer?.removeAnimation(forKey: "pulse")
        if cfg.pulse {
            let anim = CABasicAnimation(keyPath: "opacity")
            anim.fromValue = 0.35
            anim.toValue = 1.0
            anim.duration = 0.85
            anim.autoreverses = true
            anim.repeatCount = .infinity
            stateBorderLayer?.add(anim, forKey: "pulse")
        } else {
            stateBorderLayer?.opacity = 1.0
        }

        topStatusChip?.text = cfg.chipText
        topStatusChip?.setStyle(cfg.chipStyle)
        onRecordingChanged(state == .recording)
    }

    func showErrorBanner(_ text: String) {
        warningLabel?.backgroundColor = DesignTokens.Colors.destructive
        warningLabel?.textColor = DesignTokens.Colors.cardBackground
        warningLabel?.text = text
        warningLabel?.isHidden = false
    }

    func hideBanner() {
        warningLabel?.backgroundColor = DesignTokens.Colors.warning.withAlphaComponent(0.85)
        warningLabel?.textColor = DesignTokens.Colors.ink
        warningLabel?.isHidden = true
    }

    private func stateVisualConfig(for state: CameraViewController.AppState) -> StateVisualConfig {
        switch state {
        case .standby:
            return .init(
                borderColor: DesignTokens.Colors.cardBorder,
                borderWidth: 2,
                pulse: false,
                chipText: "待機",
                chipStyle: .neutral
            )
        case .timeSyncWaiting:
            return .init(
                borderColor: DesignTokens.Colors.accent,
                borderWidth: 8,
                pulse: true,
                chipText: "時間校正中",
                chipStyle: .pending
            )
        case .mutualSyncing:
            return .init(
                borderColor: DesignTokens.Colors.accent,
                borderWidth: 8,
                pulse: true,
                chipText: "互相同步中",
                chipStyle: .pending
            )
        case .recording:
            return .init(
                borderColor: DesignTokens.Colors.destructive,
                borderWidth: 14,
                pulse: false,
                chipText: "● 錄影中",
                chipStyle: .fail
            )
        }
    }
}
