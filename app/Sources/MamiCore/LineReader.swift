import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public enum WorkerTransportError: Error, Equatable, LocalizedError {
    case timedOut
    case closed
    case incompleteLine
    case oversizedLine

    public var errorDescription: String? {
        switch self {
        case .timedOut: return "The worker did not respond before its deadline."
        case .closed: return "The worker closed its output."
        case .incompleteLine: return "The worker exited with an incomplete response."
        case .oversizedLine: return "The worker response exceeded the protocol size limit."
        }
    }
}

/// One deadline covers the complete operation, including partial reads and EINTR.
public struct PipeDeadline {
    public enum Event { case readable, writable }
    private let clock: ContinuousClock
    private let end: ContinuousClock.Instant

    public init(timeout: Duration) {
        let clock = ContinuousClock()
        self.clock = clock
        end = clock.now.advanced(by: timeout)
    }

    public func wait(for descriptor: Int32, event: Event) throws {
        while true {
            let remaining = clock.now.duration(to: end)
            guard remaining > .zero else { throw WorkerTransportError.timedOut }
            let parts = remaining.components
            let milliseconds = Double(parts.seconds) * 1_000 + Double(parts.attoseconds) / 1_000_000_000_000_000
            let timeout = Int32(min(Double(Int32.max), max(1, milliseconds.rounded(.up))))
            var descriptorState = pollfd(fd: descriptor, events: Int16(event == .readable ? POLLIN : POLLOUT), revents: 0)
            let result = poll(&descriptorState, 1, timeout)
            if result < 0 {
                if errno == EINTR { continue }
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if result == 0 { continue }
            if descriptorState.revents & Int16(POLLNVAL) != 0 { throw POSIXError(.EBADF) }
            // Let the actual read/write distinguish EOF from EPIPE on HUP/ERR.
            return
        }
    }
}

public struct LineReader {
    public static let defaultMaximumLineBytes = 8 * 1024 * 1024
    private static let readChunkBytes = 64 * 1024
    private let handle: FileHandle
    private let maximumLineBytes: Int
    private var buffer = Data()

    public init(handle: FileHandle, maximumLineBytes: Int = Self.defaultMaximumLineBytes) {
        precondition(maximumLineBytes > 0 && maximumLineBytes < Int.max)
        self.handle = handle
        self.maximumLineBytes = maximumLineBytes
    }

    public mutating func readLine(timeout: Duration) throws -> Data {
        let deadline = PipeDeadline(timeout: timeout)
        while true {
            if let newline = buffer.firstIndex(of: 10) {
                let length = buffer.distance(from: buffer.startIndex, to: newline)
                guard length <= maximumLineBytes else { throw WorkerTransportError.oversizedLine }
                let line = Data(buffer[..<newline])
                buffer.removeSubrange(...newline)
                return line
            }
            guard buffer.count <= maximumLineBytes else { throw WorkerTransportError.oversizedLine }
            try deadline.wait(for: handle.fileDescriptor, event: .readable)
            let count = min(Self.readChunkBytes, maximumLineBytes + 1 - buffer.count)
            // Foundation may keep reading until the requested length is filled
            // on a pipe. One POSIX read returns the currently available bytes,
            // allowing the deadline to cover an incomplete response as intended.
            var data = Data(count: count)
            let received = data.withUnsafeMutableBytes { bytes in
                read(handle.fileDescriptor, bytes.baseAddress, count)
            }
            if received < 0 {
                if errno == EINTR || errno == EAGAIN { continue }
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            guard received > 0 else {
                throw buffer.isEmpty ? WorkerTransportError.closed : WorkerTransportError.incompleteLine
            }
            buffer.append(data.prefix(received))
        }
    }
}
