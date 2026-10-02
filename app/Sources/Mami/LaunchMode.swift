import Foundation

enum LaunchMode {
    static var isIntegrationCheck: Bool {
        CommandLine.arguments.contains { IntegrationCheck.all[$0] != nil }
    }
}
