import Foundation
import Testing
@testable import MamiCore

struct CameraConnectionsTests {
    @Test func busyDetectionDoesNotConsumeTheConnection() {
        var state = CameraConnections()
        let camera = URL(fileURLWithPath: "/Volumes/DJI", isDirectory: true)
        #expect(state.next([camera], enabled: true, busy: true) == nil)
        #expect(state.hasPending([camera]))
        #expect(state.next([camera], enabled: true, busy: false) == camera)
        #expect(state.next([camera], enabled: true, busy: false) == nil)
    }

    @Test func reconnectNotificationsNormalizeDirectoryURLs() {
        var state = CameraConnections()
        let camera = URL(fileURLWithPath: "/Volumes/DJI", isDirectory: true)
        #expect(state.next([camera], enabled: true, busy: false) == camera)
        state.disconnected(URL(fileURLWithPath: "/Volumes/DJI", isDirectory: false))
        #expect(state.next([camera], enabled: true, busy: false) == camera)
    }
}
