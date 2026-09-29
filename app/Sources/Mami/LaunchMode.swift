import Foundation

enum LaunchMode {
    static let integrationFlags: Set<String> = [
        "--ui-test", "--catalog-test", "--scan-ui-test", "--self-test",
        "--persistent-search-test", "--photos-memory-test", "--worker-pipe-test", "--worker-lifecycle-test"
    ]
    static var isIntegrationCheck: Bool {
        !integrationFlags.isDisjoint(with: CommandLine.arguments)
    }
}
