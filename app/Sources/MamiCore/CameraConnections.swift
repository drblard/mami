import Foundation

public struct CameraConnections {
    private var attempted = Set<String>()
    public init() {}
    private static func key(_ url: URL) -> String { url.standardizedFileURL.path }
    public mutating func next(_ connected: [URL], enabled: Bool, busy: Bool) -> URL? {
        attempted.formIntersection(Set(connected.map(Self.key)))
        guard enabled, !busy, let next = connected.first(where: { !attempted.contains(Self.key($0)) }) else { return nil }
        attempted.insert(Self.key(next))
        return next
    }
    public func hasPending(_ connected: [URL]) -> Bool { connected.contains { !attempted.contains(Self.key($0)) } }
    public mutating func retry() { attempted.removeAll() }
    public mutating func disconnected(_ url: URL) { attempted.remove(Self.key(url)) }
}
