import Testing
import MamiCore

struct BrowserInteractionTests {
    @Test func secondPlainClickClearsTheOnlySelection() {
        #expect(GridClickSelection.click("a", selected: [], extending: false) == ["a"])
        #expect(GridClickSelection.click("a", selected: ["a"], extending: false) == [])
        #expect(GridClickSelection.click("b", selected: ["a"], extending: false) == ["b"])
    }

    @Test func plainClickInsideMultipleSelectionNarrowsToThatItem() {
        #expect(GridClickSelection.click("a", selected: ["a", "b"], extending: false) == ["a"])
    }

    @Test func commandClickTogglesOnlyTheClickedItem() {
        #expect(GridClickSelection.click("b", selected: ["a"], extending: true) == ["a", "b"])
        #expect(GridClickSelection.click("a", selected: ["a", "b"], extending: true) == ["b"])
        #expect(GridClickSelection.click("a", selected: ["a"], extending: true) == [])
    }

    @Test func idleProgressHidesExactlyAtTheDelay() {
        let delay = ProgressVisibility.idleHideDelay
        #expect(delay == .seconds(10))
        #expect(ProgressVisibility.isVisible(active: false, needsAttention: false, sinceLastProgress: delay - .milliseconds(1)))
        #expect(!ProgressVisibility.isVisible(active: false, needsAttention: false, sinceLastProgress: delay))
    }

    @Test func activeWorkPausesAndErrorsStayVisible() {
        #expect(ProgressVisibility.isVisible(active: true, needsAttention: false, sinceLastProgress: .seconds(600)))
        #expect(ProgressVisibility.isVisible(active: false, needsAttention: true, sinceLastProgress: .seconds(600)))
    }
}

struct RangeSelectionTests {
    let order = [10, 11, 12, 13, 14, 15]

    @Test func shiftClickSelectsTheInclusiveRangeInEitherDirection() {
        #expect(RangeSelection.extend([10, 11], order: order, anchor: 11, target: 14) == [10, 11, 12, 13, 14])
        #expect(RangeSelection.extend([14], order: order, anchor: 14, target: 12) == [12, 13, 14])
    }

    @Test func shiftClickAfterDeselectingClearsTheRange() {
        #expect(RangeSelection.extend(Set(order).subtracting([11]), order: order, anchor: 11, target: 13) == [10, 14, 15])
    }

    @Test func rangeKeepsSelectionsOutsideIt() {
        #expect(RangeSelection.extend([10, 15], order: order, anchor: 10, target: 11) == [10, 11, 15])
    }

    @Test func missingAnchorSelectsOnlyTheTargetAndUnknownTargetsChangeNothing() {
        #expect(RangeSelection.extend([10], order: order, anchor: nil, target: 13) == [10, 13])
        #expect(RangeSelection.extend([10], order: order, anchor: 99, target: 13) == [10, 13])
        #expect(RangeSelection.extend([10], order: order, anchor: 11, target: 99) == [10])
    }
}
