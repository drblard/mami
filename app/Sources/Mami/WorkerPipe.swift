import Foundation
import Darwin
import MamiCore

enum WorkerPipe {
    /// A worker can exit between any liveness check and write. Suppress SIGPIPE
    /// on this descriptor so that the caller receives EPIPE instead of dying.
    /// Unlike ignoring SIGPIPE globally, this does not change child processes.
    static func write(_ data: Data, to handle: FileHandle, timeout: Duration = .seconds(5)) throws {
        let fd = handle.fileDescriptor
        guard fcntl(fd, F_SETNOSIGPIPE, 1) != -1 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let flags = fcntl(fd, F_GETFL)
        guard flags != -1, fcntl(fd, F_SETFL, flags | O_NONBLOCK) != -1 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer { _ = fcntl(fd, F_SETFL, flags) }
        let deadline = PipeDeadline(timeout: timeout)
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                try deadline.wait(for: fd, event: .writable)
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0 {
                    if errno == EINTR || errno == EAGAIN { continue }
                    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                }
                guard count > 0 else { throw POSIXError(.EIO) }
                offset += count
            }
        }
    }

    static func check() throws {
        let pipe = Pipe()
        let message = Data("{\"action\":\"busy\"}\n".utf8)
        try write(message, to: pipe.fileHandleForWriting)
        guard try pipe.fileHandleForReading.read(upToCount: message.count) == message else {
            throw AppError.message("Worker pipe did not deliver its command")
        }
        try pipe.fileHandleForReading.close()
        for _ in 0..<100 {
            do {
                try write(message, to: pipe.fileHandleForWriting)
                throw AppError.message("Closed worker pipe unexpectedly accepted a command")
            } catch let error as POSIXError where error.code == .EPIPE { }
        }
        try pipe.fileHandleForWriting.close()
        print("WORKER PIPE live delivery and 100 closed-reader writes passed without SIGPIPE")
    }
}
