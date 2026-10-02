import AppKit

/// Measures real clicks: time from mouse release (when controls act) until the main
/// thread has applied and rendered the result, i.e. the next time its run loop goes
/// idle after the change. Named actions include gesture delays; other clicks record
/// how long the main thread stayed busy. Lines go to `interaction-latency.log`.
@MainActor final class InteractionLatency {
    static let shared = InteractionLatency()
    /// An action reported this long after the release no longer belongs to that click.
    static let attributionWindow: TimeInterval = 3
    private var released: TimeInterval?
    private var monitor: Any?
    let log = BoundedLog("interaction-latency.log")

    func start() {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseUp, .keyDown]) { [weak self] event in
            // A key press starts its own action; it must not be timed from an earlier click.
            if event.type == .keyDown { self?.released = nil } else { self?.clicked(at: event.timestamp) }
            return event
        }
    }

    /// Call where a click's visible effect is applied (selection, opening, toggles).
    func applied(_ action: String) {
        guard let released, ProcessInfo.processInfo.systemUptime - released < Self.attributionWindow else { return }
        self.released = nil
        whenRendered { self.record(action, since: released) }
    }

    private func clicked(at timestamp: TimeInterval) {
        released = timestamp
        whenRendered { self.record("click (main thread)", since: timestamp) }
    }

    /// Runs once the main run loop next waits, ordered after SwiftUI's update and Core
    /// Animation's commit observers (order 2,000,000), so the change has been rendered.
    private func whenRendered(_ body: @escaping @MainActor () -> Void) {
        let observer = CFRunLoopObserverCreateWithHandler(nil, CFRunLoopActivity.beforeWaiting.rawValue, false, CFIndex.max) { observer, _ in
            CFRunLoopObserverInvalidate(observer)
            MainActor.assumeIsolated { body() }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
    }

    private func record(_ action: String, since start: TimeInterval) {
        let milliseconds = Int(((ProcessInfo.processInfo.systemUptime - start) * 1000).rounded())
        log.write("\(milliseconds)ms \(action)")
    }
}
