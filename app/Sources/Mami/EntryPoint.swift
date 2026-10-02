import AppKit
import Foundation

@main enum EntryPoint {
    @MainActor static func main() {
        let arguments = CommandLine.arguments
        if let mode = arguments.dropFirst().first, let helper = helpers[mode] {
            do { try helper(Array(arguments.dropFirst(2))); exit(0) }
            catch { fputs("\(error)\n", stderr); exit(1) }
        }
        if let (check, argument) = IntegrationCheck.requested(in: arguments) { IntegrationCheck.run(check, argument) }
        AppDiagnostics.captureAppErrors()
        MamiApp.main()
    }

    /// Helper modes the Python workers invoke on this executable (native decoding/encoding).
    private static let helpers: [String: @Sendable ([String]) throws -> Void] = [
        "--encode-text": { arguments in
            guard arguments.count == 1 else { throw AppError.message("--encode-text MODEL") }
            try NativeTextEncoder.serve(modelURL: URL(fileURLWithPath: arguments[0]))
        },
        "--video-previews": { arguments in
            guard arguments.count == 2 else { throw AppError.message("--video-previews SOURCE MANIFEST") }
            try VideoPreviews.generate(source: arguments[0], manifest: arguments[1])
        },
        "--image-face-frame": { arguments in
            guard arguments.count == 2 else { throw AppError.message("--image-face-frame SOURCE TARGET") }
            try ImageDecoding.preview(arguments[0], to: arguments[1], maxPixelSize: ImageDecoding.faceAnalysisPixelSize, quality: 0.95)
        },
        "--image-probe": { arguments in
            guard arguments.count >= 1 else { throw AppError.message("--image-probe SOURCE") }
            print(String(decoding: try JSONSerialization.data(withJSONObject: ImageDecoding.metadata(arguments[0])), as: UTF8.self))
        },
        "--image-frame": { arguments in
            guard arguments.count == 2 else { throw AppError.message("Image preview requires a target") }
            try ImageDecoding.preview(arguments[0], to: arguments[1])
        },
    ]
}
