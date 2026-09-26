import Foundation

struct CaptureRange: Equatable {
    let from: String
    let through: String
    var label: String { "\(from) – \(through)" }
    func contains(_ sortDate: String?) -> Bool {
        guard let sortDate, sortDate.count >= 8 else { return false }
        let day = String(sortDate.prefix(8))
        return day >= from.replacingOccurrences(of: "-", with: "") && day <= through.replacingOccurrences(of: "-", with: "")
    }
    static func day(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }
}

enum DateSearch {
    struct Parsed { let text: String; let range: CaptureRange? }
    static func parse(_ query: String, now: Date = Date()) throws -> Parsed {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        func match(_ pattern: String) -> (String, [String])? {
            guard let regex = try? NSRegularExpression(pattern: pattern, options: .caseInsensitive),
                  let found = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
                  let range = Range(found.range, in: text) else { return nil }
            let groups = (1..<found.numberOfRanges).map { index in
                Range(found.range(at: index), in: text).map { String(text[$0]) } ?? ""
            }
            return (String(text[..<range.lowerBound]).trimmingCharacters(in: .whitespaces), groups)
        }
        func validDay(_ value: String) -> Bool {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "yyyy-MM-dd"; f.isLenient = false
            return f.date(from: value).map { f.string(from: $0) == value } ?? false
        }
        if let (remaining, groups) = match(#"\b(?:from|between)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|and)\s+(\d{4}-\d{2}-\d{2})$"#) {
            guard validDay(groups[0]), validDay(groups[1]), groups[0] <= groups[1] else {
                throw AppError.message("Use a valid date range, with the start before the end: from 2026-09-01 to 2026-09-30")
            }
            return Parsed(text: remaining, range: CaptureRange(from: groups[0], through: groups[1]))
        }
        if let (remaining, groups) = match(#"\bon\s+(\d{4}-\d{2}-\d{2})$"#) {
            guard validDay(groups[0]) else { throw AppError.message("Use a valid date: on 2026-09-01") }
            return Parsed(text: remaining, range: CaptureRange(from: groups[0], through: groups[0]))
        }
        let months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
        let names = (months + months.map { String($0.prefix(3)) } + ["sept"]).joined(separator: "|")
        if let (remaining, groups) = match("\\bin\\s+(\(names))(?:\\s+(\\d{4}))?$") {
            let month = months.firstIndex { $0.hasPrefix(String(groups[0].lowercased().prefix(3))) }! + 1
            let calendar = Calendar.current
            let year = Int(groups[1]) ?? calendar.component(.year, from: now)
            guard (1...9999).contains(year), let start = calendar.date(from: DateComponents(year: year, month: month, day: 1)),
                  let days = calendar.range(of: .day, in: .month, for: start) else { throw AppError.message("Invalid capture year") }
            return Parsed(text: remaining, range: CaptureRange(from: String(format: "%04d-%02d-01", year, month), through: String(format: "%04d-%02d-%02d", year, month, days.count)))
        }
        if let (remaining, groups) = match(#"\bin\s+(\d{4})$"#), let year = Int(groups[0]), year > 0 {
            return Parsed(text: remaining, range: CaptureRange(from: "\(groups[0])-01-01", through: "\(groups[0])-12-31"))
        }
        return Parsed(text: text, range: nil)
    }
}
