import Foundation

public enum SearchTiming {
    public static let typingDebounce: Duration = .milliseconds(120)
    public static let visualWarmupPoll: Duration = .milliseconds(100)
    public static let visualWarmupTimeout: Duration = .seconds(30)
}
