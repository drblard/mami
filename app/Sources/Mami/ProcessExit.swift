import Foundation
import Darwin

/// A worker closes its output moments before Foundation observes its exit.
/// `terminationStatus`/`terminationReason` raise an Objective-C exception while
/// `isRunning` is still true; unwinding that through Swift concurrency on the
/// main actor leaves the app unable to run main-actor work. Wait for the exit
/// before reading either property.
enum ProcessExit {
    static let pollInterval: Duration = .milliseconds(25)

    /// Returns whether the process exited within `grace`.
    static func wait(for process: Process, grace: Duration) async -> Bool {
        let deadline = ContinuousClock.now.advanced(by: grace)
        while process.isRunning && ContinuousClock.now < deadline {
            try? await Task.sleep(for: pollInterval)
        }
        return !process.isRunning
    }

    /// Waits for a natural exit, then escalates to SIGTERM and SIGKILL, each with
    /// `grace`. Returns whether the exit has been observed.
    static func reap(_ process: Process, grace: Duration) async -> Bool {
        if await wait(for: process, grace: grace) { return true }
        process.terminate()
        if await wait(for: process, grace: grace) { return true }
        _ = Darwin.kill(process.processIdentifier, SIGKILL)
        return await wait(for: process, grace: grace)
    }

    /// Whether the process exited with status 0, or nil while it is still running.
    static func succeeded(_ process: Process) -> Bool? {
        guard !process.isRunning else { return nil }
        return process.terminationReason == .exit && process.terminationStatus == 0
    }
}
