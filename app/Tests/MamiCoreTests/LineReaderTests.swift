import Foundation
import Testing
import MamiCore

struct LineReaderTests {
    @Test func bufferedResponsesStaySeparate() throws {
        let pipe = Pipe()
        defer { try? pipe.fileHandleForReading.close(); try? pipe.fileHandleForWriting.close() }
        try pipe.fileHandleForWriting.write(contentsOf: Data("first\nsecond\n".utf8))
        var reader = LineReader(handle: pipe.fileHandleForReading, maximumLineBytes: 8)
        #expect(try reader.readLine(timeout: .seconds(1)) == Data("first".utf8))
        #expect(try reader.readLine(timeout: .seconds(1)) == Data("second".utf8))
    }

    @Test func openPipeWithPartialResponseTimesOut() throws {
        let pipe = Pipe()
        defer { try? pipe.fileHandleForReading.close(); try? pipe.fileHandleForWriting.close() }
        try pipe.fileHandleForWriting.write(contentsOf: Data("partial".utf8))
        var reader = LineReader(handle: pipe.fileHandleForReading)
        #expect(throws: WorkerTransportError.timedOut) {
            try reader.readLine(timeout: .milliseconds(30))
        }
    }

    @Test func eofDoesNotAcceptAnUnterminatedResponse() throws {
        let pipe = Pipe()
        defer { try? pipe.fileHandleForReading.close() }
        try pipe.fileHandleForWriting.write(contentsOf: Data("partial".utf8))
        try pipe.fileHandleForWriting.close()
        var reader = LineReader(handle: pipe.fileHandleForReading)
        #expect(throws: WorkerTransportError.incompleteLine) {
            try reader.readLine(timeout: .seconds(1))
        }
    }

    @Test func oversizedResponseIsRejectedBeforeUnboundedBuffering() throws {
        let pipe = Pipe()
        defer { try? pipe.fileHandleForReading.close(); try? pipe.fileHandleForWriting.close() }
        try pipe.fileHandleForWriting.write(contentsOf: Data("0123456789\n".utf8))
        var reader = LineReader(handle: pipe.fileHandleForReading, maximumLineBytes: 8)
        #expect(throws: WorkerTransportError.oversizedLine) {
            try reader.readLine(timeout: .seconds(1))
        }
    }
}
