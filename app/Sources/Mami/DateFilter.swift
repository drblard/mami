import SwiftUI

enum DateFilterMode: String, CaseIterable, Identifiable {
    case day = "Day", week = "Week", months = "Month(s)", year = "Year", range = "Custom range"
    var id: String { rawValue }
}

enum DateFilterPreset: String, CaseIterable, Identifiable {
    case today = "Today", yesterday = "Yesterday", seven = "Last 7 days", thirty = "Last 30 days", month = "This month"
    var id: String { rawValue }
    func dates(now: Date = Date(), calendar: Calendar = .current) -> (Date, Date) {
        let today = calendar.startOfDay(for: now)
        switch self {
        case .today: return (today, today)
        case .yesterday:
            let yesterday = calendar.date(byAdding: .day, value: -1, to: today)!
            return (yesterday, yesterday)
        case .seven: return (calendar.date(byAdding: .day, value: -6, to: today)!, today)
        case .thirty: return (calendar.date(byAdding: .day, value: -29, to: today)!, today)
        case .month:
            let month = calendar.dateInterval(of: .month, for: today)!
            return (month.start, calendar.date(byAdding: .day, value: -1, to: month.end)!)
        }
    }
}

struct DateFilterDraft {
    var from: Date
    var through: Date
    var anchor: Date?

    mutating func switchMode(_ mode: DateFilterMode, now: Date = Date(), calendar: Calendar = .current) {
        anchor = nil
        select(now, mode: mode, calendar: calendar)
        // The default is ready to apply. A subsequent click starts a fresh span.
        anchor = nil
    }

    static func label(from: Date, through: Date) -> String {
        let first = from.formatted(.dateTime.day().month(.abbreviated).year())
        return Calendar.current.isDate(from, inSameDayAs: through) ? first : "\(first) – \(through.formatted(.dateTime.day().month(.abbreviated).year()))"
    }

    mutating func select(_ date: Date, mode: DateFilterMode, calendar: Calendar = .current) {
        let day = calendar.startOfDay(for: date)
        switch mode {
        case .day: from = day; through = day; anchor = nil
        case .week, .year:
            let interval = calendar.dateInterval(of: mode == .week ? .weekOfYear : .year, for: day)!
            from = interval.start
            through = calendar.date(byAdding: .day, value: -1, to: interval.end)!
            anchor = nil
        case .months:
            let month = calendar.dateInterval(of: .month, for: day)!
            let first = min(anchor ?? month.start, month.start)
            let last = max(anchor ?? month.start, month.start)
            from = first
            through = calendar.date(byAdding: .day, value: -1, to: calendar.dateInterval(of: .month, for: last)!.end)!
            anchor = anchor == nil ? month.start : nil
        case .range:
            from = min(anchor ?? day, day)
            through = max(anchor ?? day, day)
            anchor = anchor == nil ? day : nil
        }
    }
}

private struct DatePresetRow: View {
    let title: String
    let action: () -> Void
    @ViewState private var hovered = false

    var body: some View {
        Button(action: action) {
            Text(title)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10).padding(.vertical, 8)
                .foregroundStyle(hovered ? Color.white : Color.primary)
                .background(hovered ? Color.blue : Color.clear, in: RoundedRectangle(cornerRadius: 5))
                .contentShape(Rectangle())
        }.buttonStyle(.plain)
            .onHover { hovered = $0 }
    }
}

struct DateFilterPopover: View {
    @ViewState private var draft: DateFilterDraft
    @ViewState private var cursor: Date
    @ViewState private var mode: DateFilterMode
    let apply: (Date, Date) -> Void
    let clear: () -> Void
    let cancel: () -> Void
    private let calendar = Calendar.current
    private let earliest: Date
    private let latest: Date

    init(from: Date, through: Date, enabled: Bool, earliest: Date? = nil, apply: @escaping (Date, Date) -> Void,
         clear: @escaping () -> Void, cancel: @escaping () -> Void) {
        let today = Calendar.current.startOfDay(for: Date())
        let lower = min(earliest.map { Calendar.current.startOfDay(for: $0) } ?? today, today)
        self.earliest = lower; self.latest = today
        let start = min(max(enabled ? from : today, lower), today)
        let end = min(max(enabled ? through : start, start), today)
        _draft = ViewState(initialValue: DateFilterDraft(from: start, through: end))
        _cursor = ViewState(initialValue: Calendar.current.dateInterval(of: .month, for: start)!.start)
        _mode = ViewState(initialValue: Calendar.current.isDate(start, inSameDayAs: end) ? .day : .range)
        self.apply = apply; self.clear = clear; self.cancel = cancel
    }

    private var year: Int { calendar.component(.year, from: cursor) }
    private var firstYear: Int { calendar.component(.year, from: earliest) }
    private var lastYear: Int { calendar.component(.year, from: latest) }
    private func clampDraft() {
        draft.from = min(max(draft.from, earliest), latest)
        draft.through = min(max(draft.through, draft.from), latest)
    }
    private func select(_ date: Date) {
        draft.select(date, mode: mode)
        clampDraft()
    }
    private var guidance: String {
        switch mode {
        case .day: return "Click a date to select one day."
        case .week: return "Click any day to select its calendar week."
        case .months: return draft.anchor == nil ? "Click a month; optionally click another to select a span." : "One month selected. Apply, or click the last month of your span."
        case .year: return "Click a year to select January through December."
        case .range: return draft.anchor == nil ? "Click the first day, then the last day." : "Click the last day, or Apply to keep a single day."
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Quick dates").font(.headline).padding(.bottom, 4)
                ForEach(DateFilterPreset.allCases) { preset in
                    DatePresetRow(title: preset.rawValue) {
                        let dates = preset.dates()
                        apply(max(dates.0, earliest), min(dates.1, latest))
                    }
                    .disabled(preset.dates().1 < earliest)
                }
                Text("Last 7 / 30 days include today.").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                Divider().padding(.vertical, 6)
                DatePresetRow(title: "All dates", action: clear)
            }.buttonStyle(.borderless).frame(width: 132, alignment: .leading)
            Divider()
            VStack(alignment: .leading, spacing: 14) {
                Text("Capture date").font(.headline)
                Picker("Selection", selection: $mode) {
                    ForEach(DateFilterMode.allCases) { Text($0.rawValue).tag($0) }
                }.pickerStyle(.segmented)
                    .onChange(of: mode) { _, value in
                        let now = Date()
                        draft.switchMode(value, now: now, calendar: calendar)
                        clampDraft()
                        cursor = calendar.dateInterval(of: .month, for: now)!.start
                    }
                HStack {
                    Button { navigate(-1) } label: { Image(systemName: "chevron.left") }
                        .accessibilityLabel("Previous calendar page")
                        .disabled(!canNavigate(-1))
                    Button { navigate(1) } label: { Image(systemName: "chevron.right") }
                        .accessibilityLabel("Next calendar page")
                        .disabled(!canNavigate(1))
                    Spacer()
                    Picker("Jump to year", selection: Binding(get: { year }, set: { value in
                        let date = calendar.date(from: DateComponents(year: value, month: calendar.component(.month, from: cursor), day: 1))!
                        cursor = calendar.dateInterval(of: .month, for: min(max(date, earliest), latest))!.start
                    })) {
                        ForEach(firstYear...lastYear, id: \.self) { Text(String($0)).tag($0) }
                    }.frame(width: 150)
                    Button("Current month") { cursor = calendar.dateInterval(of: .month, for: Date())!.start }
                }
                Group {
                    if mode == .months { monthGrid }
                    else if mode == .year { yearGrid }
                    else {
                        HStack(alignment: .top, spacing: 20) {
                            monthCalendar(cursor)
                            let next = calendar.date(byAdding: .month, value: 1, to: cursor)!
                            if next <= latest { monthCalendar(next) }
                            else { Color.clear.frame(maxWidth: .infinity) }
                        }
                    }
                }.frame(height: 250, alignment: .top)
                Text(guidance).font(.caption).foregroundStyle(.secondary).lineLimit(2).frame(height: 30, alignment: .top)
                Divider()
                Text(DateFilterDraft.label(from: draft.from, through: draft.through)).font(.headline)
                Text("Both endpoints included · limited to your library’s earliest date through today.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Clear", action: clear)
                    Spacer()
                    Button("Cancel", action: cancel).keyboardShortcut(.cancelAction)
                    Button("Apply") { apply(draft.from, draft.through) }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                }
            }.frame(width: 530)
        }.padding(20).fixedSize()
    }

    private func navigate(_ direction: Int) {
        let unit: Calendar.Component = mode == .months || mode == .year ? .year : .month
        let next = calendar.date(byAdding: unit, value: direction * (mode == .year ? 12 : 1), to: cursor)!
        cursor = calendar.dateInterval(of: .month, for: min(max(next, earliest), latest))!.start
    }

    private func canNavigate(_ direction: Int) -> Bool {
        if mode == .year {
            let first = year - year % 12 + direction * 12
            return first <= lastYear && first + 11 >= firstYear
        }
        let unit: Calendar.Component = mode == .months ? .year : .month
        let next = calendar.date(byAdding: unit, value: direction, to: cursor)!
        let interval = calendar.dateInterval(of: unit, for: next)!
        return interval.start <= latest && interval.end > earliest
    }

    private func highlighted(_ start: Date, through end: Date) -> Bool {
        start <= draft.through && end >= draft.from
    }

    private var monthGrid: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 4), spacing: 14) {
            ForEach(1...12, id: \.self) { month in
                let date = calendar.date(from: DateComponents(year: year, month: month, day: 1))!
                let end = calendar.date(byAdding: .day, value: -1, to: calendar.dateInterval(of: .month, for: date)!.end)!
                if date <= latest && end >= earliest {
                Button { select(date) } label: {
                    Text(date.formatted(.dateTime.month(.wide))).frame(maxWidth: .infinity, minHeight: 52)
                        .background(highlighted(date, through: end) ? Color.accentColor.opacity(0.3) : Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
                        .contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel(date.formatted(.dateTime.month(.wide).year()))
                }
            }
        }
    }

    private var yearGrid: some View {
        let first = year - year % 12
        return LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 4), spacing: 14) {
            ForEach(max(first, firstYear)...min(first + 11, lastYear), id: \.self) { value in
                let date = calendar.date(from: DateComponents(year: value, month: 1, day: 1))!
                let end = calendar.date(from: DateComponents(year: value, month: 12, day: 31))!
                Button { select(date) } label: {
                    Text(String(value)).frame(maxWidth: .infinity, minHeight: 52)
                        .background(highlighted(date, through: end) ? Color.accentColor.opacity(0.3) : Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
                        .contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
        }
    }

    private func monthCalendar(_ month: Date) -> some View {
        let start = calendar.dateInterval(of: .month, for: month)!.start
        let offset = (calendar.component(.weekday, from: start) - calendar.firstWeekday + 7) % 7
        let count = calendar.range(of: .day, in: .month, for: start)!.count
        return VStack(spacing: 6) {
            Text(start.formatted(.dateTime.month(.wide).year())).font(.subheadline.bold())
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 7), spacing: 3) {
                ForEach(0..<7, id: \.self) { weekday in
                    Text(calendar.veryShortStandaloneWeekdaySymbols[(calendar.firstWeekday - 1 + weekday) % 7])
                        .font(.caption).foregroundStyle(.secondary).frame(height: 22)
                }
                ForEach(0..<42, id: \.self) { cell in
                    let day = cell - offset + 1
                    if day > 0 && day <= count {
                        let date = calendar.date(byAdding: .day, value: day - 1, to: start)!
                        let selected = highlighted(date, through: date)
                        let endpoint = calendar.isDate(date, inSameDayAs: draft.from) || calendar.isDate(date, inSameDayAs: draft.through)
                        if date >= earliest && date <= latest {
                        Button { select(date) } label: {
                            Text(String(day)).font(.system(size: 13, weight: calendar.isDateInToday(date) ? .bold : .regular))
                                .frame(maxWidth: .infinity, minHeight: 27)
                                .foregroundStyle(selected && endpoint ? Color.white : Color.primary)
                                .background(selected ? Color.accentColor.opacity(endpoint ? 1 : 0.22) : Color.clear, in: RoundedRectangle(cornerRadius: 4))
                                .overlay(RoundedRectangle(cornerRadius: 4).stroke(calendar.isDateInToday(date) ? Color.accentColor : .clear))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).accessibilityLabel(date.formatted(date: .complete, time: .omitted))
                            .accessibilityAddTraits(selected ? .isSelected : [])
                        } else { Color.clear.frame(height: 27) }
                    } else { Color.clear.frame(height: 27) }
                }
            }
        }.frame(maxWidth: .infinity)
    }
}
