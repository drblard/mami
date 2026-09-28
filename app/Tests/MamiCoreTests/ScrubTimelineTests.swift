import Testing
import MamiCore

struct ScrubTimelineTests {
    @Test func sparseFramesFollowTimeRatherThanArrayPosition() {
        let timestamps = [0.5, 10.5, 80.5, 99.5]
        #expect(ScrubTimeline.nearestIndex(in: timestamps, to: 55) == 2)
        #expect(ScrubTimeline.nearestIndex(in: timestamps, to: 45.5) == 1)
        #expect(ScrubTimeline.nearestIndex(in: timestamps, to: 0) == 0)
        #expect(ScrubTimeline.nearestIndex(in: timestamps, to: 120) == 3)
    }

    @Test func missingFramesAndInvalidTargetsHaveNoScrubSelection() {
        #expect(ScrubTimeline.nearestIndex(in: [], to: 10) == nil)
        #expect(ScrubTimeline.nearestIndex(in: [1], to: .infinity) == nil)
    }
}
