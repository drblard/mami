import SwiftUI
import MamiCore

/// People review state. Edits go to the personal store first; the face worker
/// then recomputes suggestions and groups, and the view reloads its results.
@MainActor final class PeopleLibrary: ObservableObject {
    enum Focus: Hashable { case group(Int64), person(String) }
    struct Edit { let description: String; let undo: [PeopleSnapshot] }
    @Published private(set) var people: [Person] = []
    @Published private(set) var counts: [String: PersonCounts] = [:]
    @Published private(set) var groups: [FaceGroup] = []
    @Published private(set) var focus: Focus?
    @Published private(set) var faces: [FaceItem] = []
    @Published var selected = Set<Int64>() { didSet { if selected.isEmpty { selectionAnchor = nil } } }
    /// Last face clicked; Shift-click extends from here.
    private(set) var selectionAnchor: Int64?
    @Published private(set) var edits: [Edit] = []
    @Published var error: String?
    @Published private(set) var filterAssets = Set<String>()
    @Published private(set) var loaded = false
    /// The person just named from a group, so their page can explain what Mami found.
    @Published private(set) var justNamed: (person: String, faces: Int)?
    /// The last edit on a person's page and their counts before it, so the page can
    /// say what the edit did once the worker has recomputed.
    @Published private(set) var lastChange: (person: String, action: String, before: PersonCounts)?
    /// Which kind of faces the person page shows; changing it reloads them.
    @Published var personTab: PersonFaces = .toCheck {
        didSet { if personTab != oldValue { selected = []; faces = []; Task { await reload() } } }
    }
    /// Screenshots and saved/shared media mostly show strangers; off by default.
    @Published var includeSavedMedia = false { didSet { if includeSavedMedia != oldValue { Task { await reload() } } } }
    private(set) var filterPeople = Set<String>()
    private let catalog: Catalog
    private var reloading = false
    private var reloadAgain = false
    private var selectAllOnReload = false

    init(catalog: Catalog = .standard) { self.catalog = catalog }

    /// Click toggles one face; Shift-click gives the whole range the anchor's state.
    func click(_ face: Int64, extending: Bool) {
        if extending, selectionAnchor != nil {
            selected = RangeSelection.extend(selected, order: faces.map(\.id), anchor: selectionAnchor, target: face)
        } else if selected.contains(face) { selected.remove(face) } else { selected.insert(face) }
        selectionAnchor = face
    }

    func name(of id: String) -> String { people.first { $0.id == id }?.name ?? "Unknown person" }

    /// One reload at a time; a request during a reload schedules exactly one more
    /// pass with the latest state, so results are never starved or lost.
    func reload(selectAll: Bool = false) async {
        selectAllOnReload = selectAllOnReload || selectAll
        if reloading { reloadAgain = true; return }
        reloading = true
        defer { reloading = false }
        repeat {
            reloadAgain = false
            let selectAll = selectAllOnReload
            selectAllOnReload = false
            await loadOnce(selectAll: selectAll)
        } while reloadAgain
    }

    private func loadOnce(selectAll: Bool) async {
        let catalog = catalog, focus = focus, filter = filterPeople, includeSaved = includeSavedMedia, tab = personTab
        let started = ContinuousClock.now
        defer {
            let elapsed = started.duration(to: ContinuousClock.now)
            if elapsed > .seconds(2) { LaneLog.record("people", "slow reload \(elapsed) focus \(String(describing: focus))") }
        }
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                let faces: [FaceItem]
                switch focus {
                case .group(let id): faces = try FaceIndex.faces(group: id, catalog)
                case .person(let id): faces = try FaceIndex.faces(person: id, kind: tab, catalog)
                case nil: faces = []
                }
                return (try catalog.people(), try FaceIndex.counts(catalog), try FaceIndex.groups(includeSavedMedia: includeSaved, catalog), faces,
                        try FaceIndex.assets(withAll: filter, catalog))
            }.value
            people = result.0; counts = result.1; groups = result.2; filterAssets = result.4
            // Faces belong to the focus they were loaded for; a newer focus reloads again.
            if focus == self.focus && tab == personTab {
                faces = result.3
                let ids = Set(faces.map(\.id))
                selected = selectAll ? ids : selected.intersection(ids)
            } else { reloadAgain = true; selectAllOnReload = selectAllOnReload || { if case .group = self.focus { return true }; return false }() }
            if case .person(let id) = focus, !people.contains(where: { $0.id == id }) { self.focus = nil }
            loaded = true
        } catch {
            LaneLog.record("people", "reload failed: \(error.localizedDescription)")
            self.error = "Could not load people: \(error.localizedDescription)"
        }
    }

    func show(_ focus: Focus) {
        guard focus != self.focus else { return }
        if case .person(let id) = focus, justNamed?.person == id {} else { justNamed = nil }
        if case .person(let id) = focus, lastChange?.person == id {} else { lastChange = nil }
        self.focus = focus; faces = []; selected = []
        if case .group = focus { Task { await reload(selectAll: true) } } else { Task { await reload() } }
    }

    func setFilter(_ people: Set<String>) {
        filterPeople = people
        if people.isEmpty { filterAssets = [] } else { Task { await reload() } }
    }

    private func perform(_ description: String, _ changes: [PeopleChange], hiding: Set<Int64> = []) {
        var undo: [PeopleSnapshot] = []
        do {
            for change in changes { undo.append(try catalog.apply(change)) }
        } catch {
            // Keep partial multi-step edits undoable rather than leaving them hidden.
            if !undo.isEmpty { edits.append(Edit(description: description, undo: undo)) }
            self.error = "Could not save people changes: \(error.localizedDescription)"
            changed()
            return
        }
        edits.append(Edit(description: description, undo: undo))
        faces.removeAll { hiding.contains($0.id) }
        selected.subtract(hiding)
        error = nil
        changed()
    }

    private func changed() {
        if catalog.directory == Catalog.standard.directory {
            CatalogBackups.shared.schedule(urgent: true)
            Indexing.faces.recomputeFaces()
        }
        Task { await reload() }
    }

    private func newPerson(_ name: String) -> Person {
        Person(id: UUID().uuidString, name: name, created: ISO8601DateFormatter().string(from: Date()))
    }

    private func refs(_ ids: Set<Int64>) -> [FaceRef] { faces.filter { ids.contains($0.id) }.map(\.ref) }

    /// Name selected faces as a new person, or add them to an existing one.
    func assign(_ ids: Set<Int64>, toNew name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !ids.isEmpty else { return }
        if let existing = people.first(where: { $0.name.localizedCaseInsensitiveCompare(trimmed) == .orderedSame }) {
            assign(ids, to: existing.id); return
        }
        let person = newPerson(trimmed)
        let fromGroup = isGroupFocused
        perform("Name \(ids.count) faces “\(trimmed)”", [.create(person), .confirm(refs(ids), as: person.id)], hiding: hidden(ids, keptFor: person.id))
        if fromGroup { showNamed(person.id, faces: ids.count) }
    }

    func assign(_ ids: Set<Int64>, to person: String) {
        guard !ids.isEmpty else { return }
        let fromGroup = isGroupFocused
        if !fromGroup { noteChange(person, "Confirmed \(ids.count)") }
        perform("Add \(ids.count) faces to \(name(of: person))", [.confirm(refs(ids), as: person)], hiding: hidden(ids, keptFor: person))
        if fromGroup { showNamed(person, faces: ids.count) }
    }

    private func noteChange(_ person: String, _ action: String) {
        lastChange = (person, action, counts[person] ?? PersonCounts())
    }

    private var isGroupFocused: Bool { if case .group = focus { return true }; return false }

    /// After naming from a group, go to the person: their matches appear there.
    private func showNamed(_ person: String, faces: Int) {
        justNamed = (person, faces)
        personTab = .toCheck
        show(.person(person))
    }

    func reject(_ ids: Set<Int64>, from person: String) {
        guard !ids.isEmpty else { return }
        noteChange(person, "Marked \(ids.count) as not \(name(of: person))")
        perform("Not \(name(of: person)): \(ids.count) faces", [.reject(refs(ids), from: person)], hiding: ids)
    }

    func rename(_ person: String, to name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        perform("Rename to “\(trimmed)”", [.rename(person, to: trimmed)])
    }

    func merge(_ source: String, into target: String) {
        guard source != target else { return }
        let description = "Merge \(name(of: source)) into \(name(of: target))"
        perform(description, [.merge(source, into: target)])
        focus = .person(target)
        Task { await reload() }
    }

    func delete(_ person: String) {
        perform("Remove \(name(of: person))", [.delete(person)])
        focus = nil; faces = []
    }

    func undo() {
        guard let edit = edits.popLast() else { return }
        do {
            for snapshot in edit.undo.reversed() { try catalog.restore(snapshot) }
            error = nil
        } catch { self.error = "Could not undo: \(error.localizedDescription)" }
        changed()
    }

    /// Faces leave the current view unless it is the person they now belong to.
    private func hidden(_ ids: Set<Int64>, keptFor person: String) -> Set<Int64> {
        if case .person(let current) = focus, current == person { return [] }
        return ids
    }
}

struct FaceThumbnail: View {
    let path: String
    var size: CGFloat = 96
    @ViewState private var image: NSImage?
    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().scaledToFill() }
            else { Image(systemName: "person.crop.square").font(.title2).foregroundStyle(.secondary) }
        }
        .frame(width: size, height: size).background(Color.black.opacity(0.4)).clipShape(RoundedRectangle(cornerRadius: 8))
        .task(id: path) { image = await FrameCache.shared.image(path, maxPixelSize: 160) }
    }
}

struct PeopleView: View {
    @ObservedObject var model: PeopleLibrary
    @ObservedObject private var indexing = Indexing.faces
    let close: () -> Void
    @ViewState private var newName = ""
    @ViewState private var naming = false
    @ViewState private var renaming = false

    private var focusBinding: Binding<PeopleLibrary.Focus?> {
        Binding(get: { model.focus }, set: { if let focus = $0 { model.show(focus) } })
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("People").font(.title2.weight(.semibold))
                Spacer()
                if let queue = indexing.queueCounts, queue.remaining > 0 {
                    Text("Finding faces · \(queue.remaining) files left").font(.caption).foregroundStyle(.secondary)
                }
                Button { model.undo() } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                    .disabled(model.edits.isEmpty).keyboardShortcut("z", modifiers: .command)
                    .help(model.edits.last.map { "Undo: \($0.description)" } ?? "Nothing to undo")
                Button("Done", action: close).keyboardShortcut(.cancelAction)
            }.padding(14)
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.orange).padding(.horizontal, 14) }
            Divider()
            HStack(spacing: 0) {
                List(selection: focusBinding) {
                    Section("People") {
                        if model.people.isEmpty { Text("Name a group to add someone").foregroundStyle(.secondary) }
                        ForEach(model.people) { person in
                            HStack {
                                Text(person.name).lineLimit(1)
                                Spacer()
                                let counts = model.counts[person.id] ?? PersonCounts()
                                if counts.toCheckFaces > 0 {
                                    Text("\(counts.toCheckFaces)").font(.caption2.monospacedDigit()).padding(.horizontal, 5)
                                        .background(Color.accentColor.opacity(0.35), in: Capsule()).help("Suggestions to review")
                                }
                                Text("\(counts.media)").font(.caption.monospacedDigit()).foregroundStyle(.secondary).help("Media with this person")
                            }.tag(PeopleLibrary.Focus.person(person.id))
                        }
                    }
                    Section("Unnamed groups") {
                        Toggle("Show screenshots and saved media", isOn: $model.includeSavedMedia)
                            .toggleStyle(.checkbox).font(.caption)
                            .help("Screenshots, screen recordings and saved or shared media often show strangers. Named people are still found there.")
                        if model.groups.isEmpty { Text(model.loaded ? "No unnamed groups yet" : "Loading…").foregroundStyle(.secondary) }
                        ForEach(model.groups) { group in
                            HStack(spacing: 8) {
                                FaceThumbnail(path: group.cover, size: 30)
                                Text("In \(group.media) media").monospacedDigit()
                            }.tag(PeopleLibrary.Focus.group(group.id))
                                .help("\(group.count) faces in \(group.media) photos and videos")
                        }
                    }
                }.listStyle(.sidebar).frame(width: 250)
                Divider()
                detail.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 960, minHeight: 640)
        // Native control bezels sample their backdrop; paint the same opaque
        // background as the library window instead of relying on the sheet's.
        .background(Color(red: 0.08, green: 0.09, blue: 0.11))
        // A sheet is its own presentation; match the main window's dark appearance.
        .preferredColorScheme(.dark)
        .environment(\.locale, Locale(identifier: "en_US"))
        .task { await model.reload() }
        .onChange(of: indexing.catalogGeneration) { _, _ in Task { await model.reload() } }
    }

    @ViewBuilder private var detail: some View {
        switch model.focus {
        case .group:
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Unnamed group · \(model.faces.count) faces").font(.headline)
                    Spacer()
                    selectionButtons
                    nameButton(title: "Name \(model.selected.count) selected…")
                }
                Text("Faces that look least like the rest come first. You don't need to check them all: deselect any that aren't this person, then name the group.")
                    .font(.caption).foregroundStyle(.secondary)
                faceGrid(model.faces)
            }.padding(14)
        case .person(let id):
            let counts = model.counts[id] ?? PersonCounts()
            let name = model.name(of: id)
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(model.name(of: id)).font(.headline)
                    Button("Rename…") { newName = model.name(of: id); renaming = true }
                        .popover(isPresented: $renaming) {
                            VStack(alignment: .leading) {
                                AutofocusTextField(placeholder: "Name", text: $newName) { model.rename(id, to: newName); renaming = false }
                                    .frame(width: 220)
                                HStack { Spacer(); Button("Rename") { model.rename(id, to: newName); renaming = false }.buttonStyle(.borderedProminent) }
                            }.padding()
                        }
                    Menu("More") {
                        Menu("Merge into") {
                            ForEach(model.people.filter { $0.id != id }) { other in Button(other.name) { model.merge(id, into: other.id) } }
                        }
                        Divider()
                        Button("Remove person and their labels", role: .destructive) { model.delete(id) }
                    }.fixedSize()
                    Spacer()
                    Picker("", selection: $model.personTab) {
                        Text("To check (\(counts.toCheckFaces))").tag(PersonFaces.toCheck)
                        Text("Matched automatically (\(counts.automaticFaces))").tag(PersonFaces.automatic)
                        Text("Confirmed (\(counts.confirmedFaces))").tag(PersonFaces.confirmed)
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 460)
                }
                if let named = model.justNamed, named.person == id {
                    Label(counts.toCheckFaces + counts.automaticFaces > 0
                          ? "Named \(named.faces) faces as \(name). Mami found \(name) in \(counts.media) photos and videos: \(PeopleView.faces(counts.automaticFaces)) matched automatically, and \(PeopleView.faces(counts.toCheckFaces)) less certain \(counts.toCheckFaces == 1 ? "is" : "are") waiting under To check. All of them are already included in the People filter."
                          : "Named \(named.faces) faces as \(name). Mami is looking for more photos and videos of \(name)…",
                          systemImage: "sparkles")
                        .font(.callout).padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.accentColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 8))
                }
                if let change = model.lastChange, change.person == id, model.justNamed?.person != id,
                   let summary = PeopleView.changeSummary(change.action, before: change.before, after: counts) {
                    Label(summary, systemImage: "checkmark.circle").font(.callout).foregroundStyle(.secondary)
                }
                HStack {
                    Text({
                        switch model.personTab {
                        case .toCheck: return "Less certain matches, least certain first. Confirm the right ones and mark wrong ones as not \(name); you can stop whenever they look right."
                        case .automatic: return "Very similar to faces you confirmed, so they count as \(name) without checking. If one is someone else, mark it as not \(name)."
                        case .confirmed: return "Faces you named or confirmed. Move faces that are someone else."
                        }
                    }()).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    selectionButtons
                    if model.personTab != .confirmed {
                        Button("Confirm \(model.selected.count)") { model.assign(model.selected, to: id) }.disabled(model.selected.isEmpty)
                            .keyboardShortcut(.return, modifiers: .command).help("Confirm selected faces (⌘↩)")
                    }
                    Button("Not \(name)") { model.reject(model.selected, from: id) }.disabled(model.selected.isEmpty)
                        .keyboardShortcut(.delete, modifiers: .command).help("Mark selected faces as not \(name) (⌘⌫)")
                    nameButton(title: "Move to…", excluding: id)
                }
                if model.faces.isEmpty && model.loaded {
                    Text(model.personTab == .toCheck ? "Nothing to check for \(name) right now." : "No faces here yet.")
                        .foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
                } else { faceGrid(model.faces) }
            }.padding(14)
        case nil:
            ContentUnavailableView {
                Label(model.groups.isEmpty && model.people.isEmpty ? "Finding faces" : "Choose a group or person", systemImage: "person.2")
            } description: {
                Text(model.groups.isEmpty && model.people.isEmpty
                     ? "Groups of similar faces appear here as Mami scans your library."
                     : "Name a group once; Mami then suggests more media with that person for you to confirm.")
            }
        }
    }

    /// "Confirmed 6 · +4 matched automatically · To check: 1,048 (+2)", or nil until
    /// the worker's recomputed counts differ from those before the edit.
    static func changeSummary(_ action: String, before: PersonCounts, after: PersonCounts) -> String? {
        guard after != before else { return nil }
        func signed(_ value: Int) -> String { value > 0 ? "+\(value)" : "\(value)" }
        var parts = [action]
        let automatic = after.automaticFaces - before.automaticFaces
        if automatic != 0 { parts.append("\(signed(automatic)) matched automatically") }
        let toCheck = after.toCheckFaces - before.toCheckFaces
        parts.append("To check: \(after.toCheckFaces.formatted())" + (toCheck == 0 ? "" : " (\(signed(toCheck)))"))
        return parts.joined(separator: " · ")
    }

    static func faces(_ count: Int) -> String { count == 1 ? "1 more face" : "\(count) more faces" }

    private var selectionButtons: some View {
        HStack {
            Button("Select all") { model.selected = Set(model.faces.map(\.id)) }.keyboardShortcut("a", modifiers: .command)
            Button("None") { model.selected = [] }.disabled(model.selected.isEmpty)
        }.controlSize(.small)
    }

    private func nameButton(title: String, excluding: String? = nil) -> some View {
        Button(title) { newName = ""; naming = true }
            .disabled(model.selected.isEmpty)
            .popover(isPresented: $naming) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Who is this?").font(.headline)
                    AutofocusTextField(placeholder: "New person’s name", text: $newName) { model.assign(model.selected, toNew: newName); naming = false }
                        .frame(width: 240)
                    Button("Create “\(newName.trimmingCharacters(in: .whitespaces))”") { model.assign(model.selected, toNew: newName); naming = false }
                        .buttonStyle(.borderedProminent).disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty)
                    let others = model.people.filter { $0.id != excluding }
                    if !others.isEmpty {
                        Divider()
                        Text("Or add to").font(.caption).foregroundStyle(.secondary)
                        ScrollView {
                            VStack(alignment: .leading) {
                                ForEach(others) { person in
                                    Button(person.name) { model.assign(model.selected, to: person.id); naming = false }.buttonStyle(.link)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }.frame(maxHeight: 200)
                    }
                }.padding(16)
            }
    }

    private func faceGrid(_ faces: [FaceItem]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Click to select · Shift-click to select or clear a range · ⌘A selects all\(isPersonPage ? " · ⌘↩ confirms · ⌘⌫ marks as not this person" : "")")
                .font(.caption2).foregroundStyle(.secondary)
            faceScroll(faces)
        }
    }

    private var isPersonPage: Bool { if case .person = model.focus { return true }; return false }

    private func faceScroll(_ faces: [FaceItem]) -> some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 104, maximum: 120), spacing: 10)], spacing: 10) {
                ForEach(faces) { face in
                    let selected = model.selected.contains(face.id)
                    FaceThumbnail(path: face.crop, size: 104)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(selected ? Color.accentColor : .clear, lineWidth: 3))
                        .overlay(alignment: .topTrailing) {
                            Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(selected ? Color.accentColor : Color.white.opacity(0.7)).padding(5)
                        }
                        .opacity(selected ? 1 : 0.55)
                        .contentShape(Rectangle())
                        .onTapGesture { model.click(face.id, extending: NSEvent.modifierFlags.contains(.shift)) }
                        .help(face.similarity.map { String(format: "Similarity %.2f", $0) } ?? "")
                        .accessibilityLabel(selected ? "Selected face" : "Face")
                }
            }.padding(.vertical, 4)
        }
    }
}

struct PeopleFilterPicker: View {
    @ObservedObject var model: PeopleLibrary
    @ObservedObject private var faces = Indexing.faces
    @Binding var selected: Set<String>
    let manage: () -> Void
    @ViewState private var open = false
    var body: some View {
        Button { open.toggle() } label: {
            Label(selected.isEmpty ? "People" : "People (\(selected.count))", systemImage: "person.2")
        }.popover(isPresented: $open) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Filter by people").font(.headline)
                if model.people.isEmpty {
                    Text("Name people in Manage people to filter by them.").foregroundStyle(.secondary)
                }
                ScrollView {
                    LazyVStack(alignment: .leading) {
                        ForEach(model.people) { person in
                            Toggle(isOn: Binding(get: { selected.contains(person.id) }, set: { on in
                                if on { selected.insert(person.id) } else { selected.remove(person.id) }
                            })) {
                                Text("\(person.name) · \(model.counts[person.id]?.media ?? 0) media")
                            }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 240)
                Text("Shows media with all selected people, including suggestions.").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Clear") { selected = [] }.disabled(selected.isEmpty)
                    Button("Manage people…") { open = false; manage() }
                    Spacer()
                    Button("Done") { open = false }
                }
            }.padding(16).frame(width: 320)
        }
        .task { await model.reload() }
        // New worker results refresh counts and, when filtering, the matching media.
        .onChange(of: faces.catalogGeneration) { _, _ in Task { await model.reload() } }
    }
}

/// A text field that becomes the active input when its window appears. SwiftUI
/// focus state does not reliably reach into popovers, which are separate windows.
struct AutofocusTextField: NSViewRepresentable {
    let placeholder: String
    @Binding var text: String
    var submit: () -> Void = {}

    final class Field: NSTextField {
        var focusOnAppear = true
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard focusOnAppear, let window else { return }
            focusOnAppear = false
            // The popover window becomes key after insertion; focus on the next turn.
            DispatchQueue.main.async { [weak self] in
                guard let self, self.window === window else { return }
                window.makeKey()
                window.makeFirstResponder(self)
            }
        }
    }
    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: AutofocusTextField
        init(_ parent: AutofocusTextField) { self.parent = parent }
        func controlTextDidChange(_ notification: Notification) {
            if let field = notification.object as? NSTextField { parent.text = field.stringValue }
        }
        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            parent.text = control.stringValue
            parent.submit()
            return true
        }
    }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> Field {
        let field = Field(string: text)
        field.placeholderString = placeholder
        field.delegate = context.coordinator
        field.bezelStyle = .roundedBezel
        return field
    }
    func updateNSView(_ field: Field, context: Context) {
        context.coordinator.parent = self
        if field.stringValue != text { field.stringValue = text }
        field.placeholderString = placeholder
    }
}
