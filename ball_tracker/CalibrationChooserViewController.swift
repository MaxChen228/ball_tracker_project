import UIKit

/// Entry point for position calibration. Two mutually-exclusive paths —
/// the old single-screen VC dumped 5 manual handles + an "Auto (ArUco)"
/// nav button into the same view and the operator couldn't tell which
/// was actually driving the save. Now the user picks up front.
final class CalibrationChooserViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        title = "位置校正"

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "關閉",
            style: .plain,
            target: self,
            action: #selector(close)
        )

        let intro = UILabel()
        intro.text = "選擇一種校正方式"
        intro.textColor = DesignTokens.Colors.sub
        intro.font = DesignTokens.Fonts.sans(size: 14, weight: .semibold)

        let autoCard = makeCard(
            title: "自動校正（推薦）",
            subtitle: "貼 6 張 ArUco 標記到本壘板後，對準拍照即可。",
            buttonTitle: "使用自動校正  →",
            action: #selector(openAuto)
        )
        let manualCard = makeCard(
            title: "手動校正",
            subtitle: "拖曳 5 個點到本壘板邊緣與後點。",
            buttonTitle: "使用手動校正  →",
            action: #selector(openManual)
        )

        let stack = UIStackView(arrangedSubviews: [intro, autoCard, manualCard])
        stack.axis = .vertical
        stack.spacing = DesignTokens.Spacing.l
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.xl),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
        ])
    }

    /// A titled card with body + CTA. Only used twice — don't extract into
    /// its own component unless a third caller shows up.
    private func makeCard(
        title: String,
        subtitle: String,
        buttonTitle: String,
        action: Selector
    ) -> UIView {
        let card = UIView()
        card.backgroundColor = DesignTokens.Colors.surface
        card.layer.cornerRadius = DesignTokens.CornerRadius.card
        card.layer.borderWidth = 1
        card.layer.borderColor = DesignTokens.Colors.border.cgColor

        let titleLabel = UILabel()
        titleLabel.text = title
        titleLabel.font = DesignTokens.Fonts.sans(size: 18, weight: .semibold)
        titleLabel.textColor = DesignTokens.Colors.ink

        let subtitleLabel = UILabel()
        subtitleLabel.text = subtitle
        subtitleLabel.font = DesignTokens.Fonts.sans(size: 14, weight: .regular)
        subtitleLabel.textColor = DesignTokens.Colors.sub
        subtitleLabel.numberOfLines = 0

        let button = UIButton(type: .system)
        button.setTitle(buttonTitle, for: .normal)
        button.setTitleColor(DesignTokens.Colors.ok, for: .normal)
        button.titleLabel?.font = DesignTokens.Fonts.sans(size: 16, weight: .semibold)
        button.contentHorizontalAlignment = .leading
        button.addTarget(self, action: action, for: .touchUpInside)

        let stack = UIStackView(arrangedSubviews: [titleLabel, subtitleLabel, button])
        stack.axis = .vertical
        stack.spacing = DesignTokens.Spacing.s
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: DesignTokens.Spacing.l),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: DesignTokens.Spacing.l),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -DesignTokens.Spacing.l),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -DesignTokens.Spacing.l),
        ])
        return card
    }

    @objc private func openAuto() {
        navigationController?.pushViewController(AutoCalibrationViewController(), animated: true)
    }

    @objc private func openManual() {
        navigationController?.pushViewController(ManualCalibrationViewController(), animated: true)
    }

    @objc private func close() {
        dismiss(animated: true)
    }
}
