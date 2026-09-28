import Foundation

public enum ScrubTimeline {
    /// Timestamps must be ordered. Coarse previews must map to time, not their
    /// temporary position in an incomplete frame array. Ties prefer the earlier frame.
    public static func nearestIndex(in timestamps: [Double], to target: Double) -> Int? {
        guard !timestamps.isEmpty, target.isFinite else { return nil }
        var lower = 0
        var upper = timestamps.count
        while lower < upper {
            let middle = lower + (upper-lower)/2
            guard timestamps[middle].isFinite else { return nil }
            if timestamps[middle] < target { lower = middle+1 }
            else { upper = middle }
        }
        if lower == 0 { return 0 }
        if lower == timestamps.count { return timestamps.count-1 }
        return target-timestamps[lower-1] <= timestamps[lower]-target ? lower-1 : lower
    }
}
