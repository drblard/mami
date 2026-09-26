import Foundation

/// One exporter and one verified-copy consumer. In-flight batches count toward
/// the limit until acknowledged, so slow storage applies backpressure to Photos.
final class PhotosPipeline: @unchecked Sendable {
    private let condition = NSCondition()
    private var queue: [(URL, Int64)] = []
    private var count = 0
    private var bytes: Int64 = 0
    private var finished = false
    private var failure: Error?
    private let cancellation: PhotosCancellation
    init(cancellation: PhotosCancellation) { self.cancellation = cancellation }
    func enqueue(_ url: URL, size: Int64) throws {
        condition.lock(); defer { condition.unlock() }
        while count >= 8 || (count > 0 && bytes + size > 512 * 1024 * 1024) {
            try cancellation.check()
            if let failure { throw failure }
            condition.wait(until: Date().addingTimeInterval(0.25))
        }
        try cancellation.check()
        queue.append((url, size)); count += 1; bytes += size
        condition.broadcast()
    }
    func take() throws -> [(URL, Int64)]? {
        condition.lock(); defer { condition.unlock() }
        while queue.isEmpty && !finished {
            try cancellation.check()
            condition.wait(until: Date().addingTimeInterval(0.25))
        }
        if let failure { throw failure }
        try cancellation.check()
        if queue.isEmpty { return nil }
        let batch = queue; queue = []
        return batch
    }
    func acknowledge(_ batch: [(URL, Int64)]) {
        condition.lock(); defer { condition.unlock() }
        count -= batch.count; bytes -= batch.reduce(0) { $0 + $1.1 }
        condition.broadcast()
    }
    func finish(_ error: Error? = nil) {
        condition.lock(); defer { condition.unlock() }
        finished = true; failure = error; condition.broadcast()
    }
}
