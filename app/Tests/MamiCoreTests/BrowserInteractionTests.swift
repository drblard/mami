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
