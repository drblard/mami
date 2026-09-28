import Foundation

public enum BackupSchedulePolicy {
    public static let manualDebounce: Duration = .seconds(2)
    public static let minimumAutomaticInterval: Duration = .seconds(60)

    public static func delay(urgent: Bool, elapsedSinceCompletion: Duration?) -> Duration {
        guard !urgent, let elapsedSinceCompletion else { return manualDebounce }
        return max(manualDebounce, minimumAutomaticInterval - elapsedSinceCompletion)
    }
}
