import Foundation

public enum GridClickSelection {
    /// A plain click selects one item; clicking the only selected item clears it.
    /// Command-click adds or removes the item without changing the rest.
    public static func click(_ id: String, selected: Set<String>, extending: Bool) -> Set<String> {
        var next = selected
        if extending {
            if next.insert(id).inserted == false { next.remove(id) }
            return next
        }
        return selected == [id] ? [] : [id]
    }
}

public enum ProgressVisibility {
    /// How long a finished or stalled progress lane remains on screen.
    public static let idleHideDelay: Duration = .seconds(10)

    /// Active work, pauses and failures remain visible because they carry the
    /// controls needed to resume or retry. Otherwise progress fades after idling.
    public static func isVisible(active: Bool, needsAttention: Bool, sinceLastProgress: Duration,
                                 hideDelay: Duration = idleHideDelay) -> Bool {
        active || needsAttention || sinceLastProgress < hideDelay
    }
}

public enum RangeSelection {
    /// Shift-click: give every item between the anchor and the target (inclusive,
    /// in display order) the anchor's state, so a range can be selected or cleared.
    /// Without a usable anchor, only the target is selected.
    public static func extend<ID: Hashable>(_ selected: Set<ID>, order: [ID], anchor: ID?, target: ID) -> Set<ID> {
        guard let end = order.firstIndex(of: target) else { return selected }
        guard let anchor, let start = order.firstIndex(of: anchor) else { return selected.union([target]) }
        let range = order[min(start, end)...max(start, end)]
        return selected.contains(anchor) ? selected.union(range) : selected.subtracting(range)
    }
}
