import DroverKit
import UIKit

/// Transient directional feedback for hold-and-drag terminal navigation.
/// The active arrow uses Drover's teal accent; the three dots show the
/// current repeat gear, matching the gesture's increasing speed with distance.
@MainActor
final class TerminalDirectionOverlay: UIView {
    private let arrows: [TerminalNavigationDirection: UIImageView]
    private let gearDots: [UIView]

    override init(frame: CGRect) {
        func arrow(_ name: String) -> UIImageView {
            let view = UIImageView(image: UIImage(systemName: name))
            view.contentMode = .scaleAspectFit
            view.tintColor = .secondaryLabel
            view.translatesAutoresizingMaskIntoConstraints = false
            return view
        }

        let up = arrow("arrow.up")
        let down = arrow("arrow.down")
        let left = arrow("arrow.left")
        let right = arrow("arrow.right")
        arrows = [.up: up, .down: down, .left: left, .right: right]
        gearDots = (0..<3).map { _ in
            let dot = UIView()
            dot.backgroundColor = .tertiaryLabel
            dot.layer.cornerRadius = 2.5
            dot.translatesAutoresizingMaskIntoConstraints = false
            return dot
        }

        super.init(frame: frame)
        isUserInteractionEnabled = false
        isAccessibilityElement = false
        backgroundColor = UIColor.secondarySystemBackground.withAlphaComponent(0.94)
        layer.cornerRadius = 16
        layer.borderWidth = 1
        layer.borderColor = UIColor.separator.cgColor
        layer.shadowColor = UIColor.black.cgColor
        layer.shadowOpacity = 0.2
        layer.shadowRadius = 10
        layer.shadowOffset = CGSize(width: 0, height: 4)

        for view in arrows.values { addSubview(view) }
        let dots = UIStackView(arrangedSubviews: gearDots)
        dots.axis = .horizontal
        dots.spacing = 4
        dots.translatesAutoresizingMaskIntoConstraints = false
        addSubview(dots)

        NSLayoutConstraint.activate([
            up.centerXAnchor.constraint(equalTo: centerXAnchor),
            up.topAnchor.constraint(equalTo: topAnchor, constant: 10),
            down.centerXAnchor.constraint(equalTo: centerXAnchor),
            down.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -10),
            left.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            left.centerYAnchor.constraint(equalTo: centerYAnchor),
            right.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            right.centerYAnchor.constraint(equalTo: centerYAnchor),
            dots.centerXAnchor.constraint(equalTo: centerXAnchor),
            dots.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        for view in arrows.values {
            NSLayoutConstraint.activate([
                view.widthAnchor.constraint(equalToConstant: 28),
                view.heightAnchor.constraint(equalToConstant: 28),
            ])
        }
        for dot in gearDots {
            NSLayoutConstraint.activate([
                dot.widthAnchor.constraint(equalToConstant: 5),
                dot.heightAnchor.constraint(equalToConstant: 5),
            ])
        }

        alpha = 0
        transform = CGAffineTransform(scaleX: 0.9, y: 0.9)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func show() {
        UIView.animate(withDuration: 0.12) {
            self.alpha = 1
            self.transform = .identity
        }
    }

    func setMotion(_ motion: TerminalNavigationMotion?) {
        for (direction, view) in arrows {
            let active = direction == motion?.direction
            view.tintColor = active ? .systemTeal : .secondaryLabel
            view.transform = active
                ? CGAffineTransform(scaleX: 1.18, y: 1.18) : .identity
        }
        let activeDots: Int
        switch motion?.gear {
        case .slow: activeDots = 1
        case .medium: activeDots = 2
        case .fast: activeDots = 3
        case nil: activeDots = 0
        }
        for (index, dot) in gearDots.enumerated() {
            dot.backgroundColor = index < activeDots ? .systemTeal : .tertiaryLabel
        }
    }

    func hideAndRemove() {
        UIView.animate(withDuration: 0.1, animations: {
            self.alpha = 0
            self.transform = CGAffineTransform(scaleX: 0.94, y: 0.94)
        }, completion: { _ in
            self.removeFromSuperview()
        })
    }
}
