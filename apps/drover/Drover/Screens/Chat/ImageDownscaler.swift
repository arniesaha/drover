import UIKit

/// Downscales picked images to the vision-model sweet spot before they ride
/// a JSON body over cellular/relay (relay hosts cap frames at 8 MiB).
enum ImageDownscaler {
    static func jpegData(from original: Data,
                         maxDimension: CGFloat = 1568,
                         quality: CGFloat = 0.7) -> Data? {
        guard let image = UIImage(data: original) else { return nil }
        let scale = min(1, maxDimension / max(image.size.width, image.size.height))
        guard scale < 1 else { return image.jpegData(compressionQuality: quality) }
        let target = CGSize(width: image.size.width * scale,
                            height: image.size.height * scale)
        let resized = UIGraphicsImageRenderer(size: target).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return resized.jpegData(compressionQuality: quality)
    }
}
