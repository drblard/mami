import Testing
import MamiCore

struct BackupSchedulePolicyTests {
    @Test func firstAndManualRequestsUseDebounce() {
        #expect(BackupSchedulePolicy.delay(urgent: false, elapsedSinceCompletion: nil) == .seconds(2))
        #expect(BackupSchedulePolicy.delay(urgent: true, elapsedSinceCompletion: .seconds(1)) == .seconds(2))
    }

    @Test func automaticRequestsRespectMinimumInterval() {
        #expect(BackupSchedulePolicy.delay(urgent: false, elapsedSinceCompletion: .zero) == .seconds(60))
        #expect(BackupSchedulePolicy.delay(urgent: false, elapsedSinceCompletion: .seconds(20)) == .seconds(40))
        #expect(BackupSchedulePolicy.delay(urgent: false, elapsedSinceCompletion: .seconds(59)) == .seconds(2))
        #expect(BackupSchedulePolicy.delay(urgent: false, elapsedSinceCompletion: .seconds(120)) == .seconds(2))
    }
}
