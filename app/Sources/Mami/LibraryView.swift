import SwiftUI
import AVKit
import ImageIO
import MamiCore

struct LibraryView: View {
    @ViewState private var library: Library
    @ViewState private var clips: ClipSelection
    @ViewState private var showClips = true
    @ViewState private var navigation: BrowserSelection
    private var selection: Selection? {
        get { navigation.preview }
        nonmutating set { navigation.preview = newValue }
    }
    private var focusedMedia: Media? {
        get { navigation.focused }
        nonmutating set { navigation.focused = newValue }
    }
    @ViewState private var commandHover = false
    @ViewState private var nearby = true
    @ViewState private var kind = "all"
    @ViewState private var sort = "default"
    @ViewState private var favoritesOnly = false
    @ViewState private var selectedLabels = Set<String>()
    @ViewState private var selectedPeople = Set<String>()
    @ViewState private var showPeople = false
    @ViewState private var people: PeopleLibrary
    @ViewState private var showImport = false
    @ViewState private var showDateRange = false
    @ViewState private var showShortcuts = false
    @ViewState private var annotations: Annotations
    private let backups = CatalogBackups.shared
    @ViewState private var scrollTarget: String?
    @ViewState private var grid = GridItems()
    @ViewState private var pendingTrash: [Media]?
    @ViewState private var trashing = false
    @ViewState private var trashError: String?
    @FocusState private var searchFocused: Bool
    private var visibleItems: [Media] {
        let annotated = favoritesOnly || !selectedLabels.isEmpty
        let filtered = library.items.filter {
            guard !library.deletedAssets.contains($0.assetID), kind == "all" || $0.kind == kind,
                  library.matchesFormat($0), library.matchesDevice($0), library.matchesDate($0),
                  selectedPeople.isEmpty || people.filterAssets.contains($0.assetID) else { return false }
            guard annotated else { return true }
            let value = annotations.value(for: $0)
            return (!favoritesOnly || value.favorite)
                && (selectedLabels.isEmpty || selectedLabels.contains(value.place) || value.tags.contains(where: selectedLabels.contains))
        }
        // Browsing pages arrive in capture order (oldest-first applied by the query); only matches need sorting.
        if library.showingMatches ? sort == "default" : library.usesProjection { return filtered }
        return filtered.sorted {
            let a = $0.metadata?.sortDate ?? "", b = $1.metadata?.sortDate ?? ""
            if a == b { return $0.path < $1.path }
            if a.isEmpty { return false }; if b.isEmpty { return true }
            return sort == "oldest" ? a < b : a > b
        }
    }
    private var availableLabels: [String] { Set(annotations.values.values.flatMap { $0.tags + [$0.place] }.filter { !$0.isEmpty }).sorted() }
    private func updatePagedFilters() {
        guard library.usesProjection else { return }
        library.browseKind = kind == "all" ? nil : kind
        library.oldestFirst = sort == "oldest"
        var assets: Set<String>? = nil
        if favoritesOnly || !selectedLabels.isEmpty {
            assets = Set(annotations.values.compactMap { asset, value in
                let labels = Set(value.tags + [value.place])
                return (!favoritesOnly || value.favorite) && (selectedLabels.isEmpty || !labels.isDisjoint(with: selectedLabels)) ? asset : nil
            })
        }
        if !selectedPeople.isEmpty { assets = assets.map { $0.intersection(people.filterAssets) } ?? people.filterAssets }
        library.browseAssets = assets
        library.search()
    }
    private var hasFilters: Bool { kind != "all" || library.format != .all || library.deviceFilter != "All devices" || library.dateEnabled || library.queryDates != nil || favoritesOnly || !selectedLabels.isEmpty || !selectedPeople.isEmpty }
    private func clearFilters() {
        kind = "all"; library.format = .all; library.deviceFilter = "All devices"; favoritesOnly = false; selectedLabels = []; selectedPeople = []
        library.dateEnabled = false
        if let parsed = try? DateSearch.parse(library.query) { library.query = parsed.text }
        library.search()
    }
    private func focus(_ media: Media) {
        navigation.select(media, extending: false); searchFocused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
    }
    private func selectCard(_ media: Media) {
        // The second click of a double-click opens the preview; it must not deselect.
        // clickCount is only defined for mouse events.
        if let event = NSApp.currentEvent, [NSEvent.EventType.leftMouseDown, .leftMouseUp].contains(event.type), event.clickCount > 1 {
            navigation.select(media, extending: false)
        }
        else { navigation.click(media, extending: NSEvent.modifierFlags.contains(.command)) }
        searchFocused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
        InteractionLatency.shared.applied("select")
    }
    private var highlightedItems: [Media] { visibleItems.filter { navigation.selectedIDs.contains($0.id) } }
    /// The previewed item, else the selection. A right-clicked card outside the selection stands alone.
    private func trashTargets(for media: Media? = nil) -> [Media] {
        if let previewed = selection?.media { return [previewed] }
        if let media, !navigation.selectedIDs.contains(media.id) { return [media] }
        return highlightedItems
    }
    private func requestTrash(_ items: [Media]) {
        guard !items.isEmpty, !trashing else { return }
        trashError = nil
        pendingTrash = items
    }
    private func trashTitle(_ items: [Media]) -> String {
        guard items.count == 1 else { return "Move \(items.count) items to the Trash?" }
        return items[0].kind == "image" ? "Move this photo to the Trash?" : "Move this video to the Trash?"
    }
    private func moveToTrash(_ items: [Media]) {
        trashing = true
        let previewScope = navigation.previewItems ?? visibleItems
        Task {
            defer { trashing = false }
            do {
                let configuration = try Configuration.load()
                let outcome = try await MediaDeletion.moveToTrash(items, configuration: configuration)
                let removed = Set(items.map(\.id))
                if let current = selection, removed.contains(current.media.id) {
                    // Keep reviewing takes: show the next remaining item, else close.
                    let remaining = previewScope.filter { !outcome.assets.contains($0.assetID) }
                    let index = previewScope.firstIndex { $0.id == current.media.id } ?? 0
                    let next = previewScope[index...].first { !outcome.assets.contains($0.assetID) } ?? remaining.last
                    if navigation.previewItems != nil { navigation.previewItems = remaining.isEmpty ? nil : remaining }
                    selection = next.map { Selection(media: $0, timestamp: $0.match.timestamp) }
                    if let next, navigation.previewItems == nil { navigation.selectedIDs = [next.id] }
                }
                navigation.selectedIDs.subtract(removed)
                if let focused = focusedMedia, removed.contains(focused.id) { focusedMedia = nil }
                library.forget(outcome.assets)
                for asset in outcome.assets where clips.items.contains(where: { $0.assetID == asset }) { clips.remove(asset) }
                CatalogUpdates.shared.notify()
            } catch { trashError = error.localizedDescription }
        }
    }
    private func open(_ media: Media, timestamp: Double?) {
        focus(media); navigation.previewItems = nil; selection = Selection(media: media, timestamp: timestamp)
        InteractionLatency.shared.applied("open preview")
    }
    private func handleKey(_ code: UInt16) -> Bool {
        guard !showImport else { return false }
        if code == 191 { showShortcuts.toggle(); return true }
        if code == 53, selection != nil { selection = nil; return true }
        if code == 11 {
            let targets = selection.map { [$0.media] } ?? highlightedItems
            clips.toggle(targets.isEmpty ? focusedMedia.map { [$0] } ?? [] : targets)
            return true
        }
        if code == 49 {
            if selection != nil { selection = nil }
            else if highlightedItems.count > 1 {
                navigation.previewItems = highlightedItems
                let first = highlightedItems.first { $0.id == focusedMedia?.id } ?? highlightedItems[0]
                selection = Selection(media: first, timestamp: first.match.timestamp)
            }
            else if let item = highlightedItems.first { open(item, timestamp: item.match.timestamp) }
            else if let item = focusedMedia, visibleItems.contains(where: { $0.id == item.id }) { open(item, timestamp: item.match.timestamp) }
            else if let item = visibleItems.first { open(item, timestamp: item.match.timestamp) }
            return true
        }
        guard [123, 124, 125, 126].contains(code) else { return false }
        if selection != nil, [125, 126].contains(code) { return true }
        let items = selection != nil ? navigation.previewItems ?? visibleItems : visibleItems
        guard !items.isEmpty else { return false }
        let offset = (code == 123 || code == 126) ? -1 : 1
        let current = selection?.media ?? focusedMedia
        let index = current.flatMap { item in items.firstIndex { $0.id == item.id } }
        let target = index.map {
            selection == nil ? GridNavigation.target(index: $0, key: code, count: items.count, columns: navigation.columns)
                : max(0, min(items.count - 1, $0 + offset))
        } ?? 0
        if selection != nil {
            guard index != target else { NSSound.beep(); return true }
            focusedMedia = items[target]
            if navigation.previewItems == nil { navigation.selectedIDs = [items[target].id] }
            selection = Selection(media: items[target], timestamp: items[target].match.timestamp)
        } else { focus(items[target]); scrollTarget = items[target].id }
        return true
    }
    private func previewPosition(_ selection: Selection, items: [Media]) -> String {
        let scoped = navigation.previewItems ?? items
        guard let index = scoped.firstIndex(where: { $0.id == selection.media.id }) else { return "" }
        return "\(index + 1) / \(scoped.count)"
    }
    private func adjacent(to selection: Selection, offset: Int) -> Media? {
        let items = visibleItems
        guard let index = items.firstIndex(where: { $0.id == selection.media.id }), items.indices.contains(index + offset) else { return nil }
        return items[index + offset]
    }
    @MainActor init(library: Library? = nil, clips: ClipSelection? = nil, navigation: BrowserSelection? = nil,
                    annotations: Annotations? = nil, people: PeopleLibrary? = nil) {
        _library = ViewState(initialValue: library ?? Library())
        _clips = ViewState(initialValue: clips ?? ClipSelection())
        _navigation = ViewState(initialValue: navigation ?? BrowserSelection())
        _annotations = ViewState(initialValue: annotations ?? Annotations())
        _people = ViewState(initialValue: people ?? PeopleLibrary())
    }
    var body: some View {
        let displayed = visibleItems
        let positions = Dictionary(uniqueKeysWithValues: displayed.enumerated().map { ($0.element.id, $0.offset) })
        let highlighted = displayed.filter { navigation.selectedIDs.contains($0.id) }
        let clipped = Set(clips.items.map(\.assetID))
        let scrubNearby = (nearby && library.showingMatches) != commandHover
        let dragEnabled = selection == nil && !showImport
        let _ = grid.items = displayed
        VStack(spacing: 0) {
            VStack(spacing: 14) {
                HStack {
                    Text("Mami").font(.system(size: 25, weight: .semibold, design: .rounded))
                    Spacer()
                    if !highlighted.isEmpty {
                        Button(role: .destructive) { requestTrash(highlighted) } label: {
                            Label("\(highlighted.count)", systemImage: "trash")
                        }.disabled(trashing).help("Move selected media to the Trash (⌘⌫)")
                            .accessibilityLabel("Move \(highlighted.count) selected to the Trash")
                    }
                    Button { showShortcuts.toggle() } label: { Image(systemName: "questionmark.circle") }
                        .help("Keyboard shortcuts (?)").accessibilityLabel("Keyboard shortcuts")
                        .popover(isPresented: $showShortcuts) { ShortcutHelp { showShortcuts = false } }
                    Button("Import media…") { showImport = true }
                    Button { showClips.toggle() } label: { Label("\(clips.items.count)", systemImage: "sidebar.right") }
                        .help("Show or hide selected clips").accessibilityLabel("Selected clips, \(clips.items.count)")
                }
                HStack(spacing: 14) {
                    Image(systemName: "magnifyingglass").font(.title2).foregroundStyle(.secondary)
                    TextField(library.mode == "speech" ? "Find words spoken in a video…" : "What are you looking for?", text: $library.query)
                        .font(.system(size: 21)).textFieldStyle(.plain).onSubmit { library.search() }
                        .focused($searchFocused)
                        .accessibilityLabel("Search your media")
                        // Start search (and its 2–3 s visual model load) while the query is typed.
                        .onChange(of: searchFocused) { _, focused in if focused { library.prepareSearch() } }
                        .onChange(of: library.query) { _, query in if !query.isEmpty { library.prepareSearch() } }
                        .task(id: library.query) {
                            do { try await Task.sleep(for: SearchTiming.typingDebounce) } catch { return }
                            guard library.ready, !Task.isCancelled else { return }
                            library.search()
                        }
                    if !library.query.isEmpty {
                        Button { library.query = ""; library.search() } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary).help("Clear search")
                    }
                    ProgressView().controlSize(.small).frame(width: 18, height: 18).opacity(library.searching ? 1 : 0)
                    Button("Search") { library.search() }.buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(!library.ready || library.searching)
                }
                .padding(.horizontal, 20).padding(.vertical, 15)
                .background(Color(red: 0.16, green: 0.17, blue: 0.20), in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white.opacity(0.12), lineWidth: 1))
                .frame(maxWidth: 820).frame(maxWidth: .infinity)
                if library.speechAvailable {
                    Picker("Search", selection: $library.mode) {
                        Text("Visuals & speech").tag("both")
                        Text("Visuals only").tag("visual")
                        Text("Speech only").tag("speech")
                    }.pickerStyle(.segmented).frame(maxWidth: 440)
                        .accessibilityLabel("Search content")
                        .onChange(of: library.mode) { _, _ in library.search() }
                }
            }.padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 18)
            HStack {
                    Text(library.usesProjection && !library.showingMatches ? "\(library.totalMediaCount) media · \(displayed.count) loaded" : "\(displayed.count) media").monospacedDigit()
                Spacer()
            }.font(.caption).padding(.horizontal, 14).padding(.bottom, 10)
            HStack {
                Picker("Show", selection: $kind) {
                    Text("All media").tag("all")
                    Text("Videos").tag("video")
                    Text("Photos").tag("image")
                }.pickerStyle(.segmented).labelsHidden().frame(width: 220)
                Divider().frame(height: 20)
                ForEach([MediaFormat.vertical, .horizontal]) { shape in
                    Toggle(isOn: Binding(get: { library.format == shape }, set: { library.format = $0 ? shape : .all })) {
                        Label(shape.label, systemImage: shape == .vertical ? "rectangle.portrait" : "rectangle")
                    }
                        .toggleStyle(.button)
                }
                Spacer()
                Toggle(isOn: $favoritesOnly) { Image(systemName: "heart.fill") }.toggleStyle(.button).help("Show favorites only").accessibilityLabel("Favorites only")
            }.padding(.horizontal, 24).padding(.bottom, 10)
            HStack {
                TagFilterPicker(available: availableLabels, selected: $selectedLabels)
                PeopleFilterPicker(model: people, selected: $selectedPeople) { showPeople = true }
                    .sheet(isPresented: $showPeople) { PeopleView(model: people) { showPeople = false } }
                Button { showDateRange = true } label: {
                    Label(library.dateEnabled ? DateFilterDraft.label(from: library.dateFrom, through: library.dateThrough) : "Date", systemImage: "calendar")
                }
                    .tint(library.dateEnabled ? Color.accentColor : Color.secondary)
                    .popover(isPresented: $showDateRange) {
                        DateFilterPopover(from: library.dateFrom, through: library.dateThrough, enabled: library.dateEnabled, earliest: library.earliestCaptureDate,
                            apply: { from, through in
                                library.dateFrom = from; library.dateThrough = through
                                library.dateEnabled = true; library.search(); showDateRange = false
                            }, clear: { library.dateEnabled = false; library.search(); showDateRange = false },
                            cancel: { showDateRange = false })
                    }
                Picker("Camera", selection: $library.deviceFilter) {
                    Text("All devices").tag("All devices")
                    ForEach(library.devices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(maxWidth: 210).onChange(of: library.deviceFilter) { _, _ in library.search() }
                if favoritesOnly { Text("Favorites").font(.caption).foregroundStyle(.pink) }
                if hasFilters { Button("Clear filters") { clearFilters() }.font(.caption) }
                Spacer()
                if library.showingMatches {
                    Toggle("Scrub ±8 seconds around match", isOn: $nearby).toggleStyle(.switch).controlSize(.small)
                }
                Picker("Sort", selection: $sort) {
                    Text(library.showingMatches ? "Best match" : "Newest first").tag("default")
                    Text("Date: newest").tag("newest")
                    Text("Oldest first").tag("oldest")
                }.labelsHidden().frame(width: 135)
            }.controlSize(.regular).padding(.horizontal, 24).padding(.bottom, 12)
            if let error = library.error { Text(error).foregroundStyle(.red).padding() }
            if let error = annotations.error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            if let trashError { Text(trashError).foregroundStyle(.red).font(.caption).padding() }
            Divider()
            HStack(spacing: 0) {
            ScrollViewReader { proxy in
            ScrollView {
                if displayed.isEmpty, library.ready, !library.searching {
                    ContentUnavailableView {
                        Label(hasFilters ? "No media fits these filters" : "No search results", systemImage: "magnifyingglass")
                    } description: {
                        Text(hasFilters ? "Try another shape or clear the filters to see more of your library." : "Try fewer words. Speech search finds Romanian words within a single spoken passage; accents are optional.")
                    } actions: {
                        if hasFilters { Button("Clear filters") { clearFilters() } }
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 240, maximum: 360), spacing: 14)], spacing: 14) {
                    ForEach(displayed) { media in
                        MediaCard(media: media, projection: library.projectionReader, nearby: scrubNearby,
                                  annotation: annotations.value(for: media), annotationsReady: annotations.ready,
                                  inClips: clipped.contains(media.assetID), clipsReady: clips.ready,
                                  focused: navigation.selectedIDs.contains(media.id), dragEnabled: dragEnabled,
                                  toggleFavorite: { annotations.toggleFavorite(media); InteractionLatency.shared.applied("favorite") },
                                  toggleClip: { clips.toggle(media, sample: $0); InteractionLatency.shared.applied("selected clips") },
                                  select: { selectCard(media) },
                                  dragItems: { navigation.itemsForDrag(media, in: grid.items) },
                                  open: { open($0, timestamp: $1) },
                                  trash: { requestTrash(trashTargets(for: media)) })
                            .equatable().id(media.id)
                            .onAppear {
                                if media.id == library.items.last?.id { Task { await library.loadMore() } }
                            }
                            .task(id: media.match.cacheKey) {
                                guard let position = positions[media.id] else { return }
                                let margin = max(3, navigation.columns * 2)
                                for index in max(0, position - margin)..<min(displayed.count, position + margin + 1) {
                                    guard !Task.isCancelled else { return }
                                    _ = await FrameCache.shared.image(displayed[index].match.frame, crop: displayed[index].match.crop)
                                }
                            }
                    }
                }
                .onGeometryChange(for: Int.self) { geometry in
                    max(1, Int((geometry.size.width + 14) / (240 + 14)))
                } action: { navigation.columns = $0 }
                .padding(14)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
                .onChange(of: scrollTarget) { _, id in
                    if let id { proxy.scrollTo(id, anchor: .center) }
                }
            }
            if showClips {
                SelectionIsland(clips: clips, projection: library.projectionReader, dragged: { MediaDragRouter.shared.draggedMedia }, open: { clip in
                    let media = library.catalogMedia.first { $0.assetID == clip.assetID } ?? clip.media
                    open(media, timestamp: clip.timestamp)
                }, collapse: { showClips = false })
            }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            LibraryFooter(library: library)
        }
        .frame(minWidth: 850, minHeight: 600)
        .background(Color(red: 0.08, green: 0.09, blue: 0.11))
        .preferredColorScheme(.dark)
        .environment(\.locale, Locale(identifier: "en_US"))
        .background(BrowserKeys(command: { commandHover = $0 }, key: handleKey, mouse: { point, size in
            guard selection != nil, !showImport,
                  !CGRect(origin: .zero, size: size).insetBy(dx: 20, dy: 20).contains(point) else { return false }
            selection = nil
            return true
        }).frame(width: 0, height: 0))
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)) { _ in commandHover = false }
        .onChange(of: library.format) { _, _ in library.search() }
        .onChange(of: kind) { _, _ in updatePagedFilters() }
        .onChange(of: sort) { _, _ in updatePagedFilters() }
        .onChange(of: favoritesOnly) { _, _ in updatePagedFilters() }
        .onChange(of: selectedLabels) { _, _ in updatePagedFilters() }
        .onChange(of: selectedPeople) { _, value in people.setFilter(value); updatePagedFilters() }
        .onChange(of: people.filterAssets) { _, _ in if !selectedPeople.isEmpty { updatePagedFilters() } }
        .onChange(of: annotations.values) { _, _ in if favoritesOnly || !selectedLabels.isEmpty { updatePagedFilters() } }
        .onChange(of: library.ready) { _, ready in if ready { updatePagedFilters() } }
        .background(Button("Focus search") { searchFocused = true }.keyboardShortcut("f", modifiers: .command).hidden())
        // Text fields keep ⌘⌫ for deleting to the start of the line.
        .background(Button("Move to Trash") { requestTrash(trashTargets()) }.keyboardShortcut(.delete, modifiers: .command)
            .disabled(searchFocused || showImport || trashing || (selection == nil && highlighted.isEmpty)).hidden())
        .alert(pendingTrash.map(trashTitle) ?? "", isPresented: Binding(get: { pendingTrash != nil }, set: { if !$0 { pendingTrash = nil } }),
               presenting: pendingTrash) { items in
            Button("Move to Trash", role: .destructive) { moveToTrash(items) }
            Button("Cancel", role: .cancel) { }
        } message: { items in
            Text("\(items.count == 1 ? "It is" : "They are") removed from Mami and the \(items.count == 1 ? "original moves" : "originals move") to the Trash. Until you empty the Trash, Put Back in Finder restores \(items.count == 1 ? "it" : "them").")
        }
        .task { await library.load() }
        .task { await annotations.load() }
        .task { await clips.load() }
        .task { if !LaunchMode.isIntegrationCheck { PhotosImporting.shared.startAutomatic() } }
        .task { if !LaunchMode.isIntegrationCheck { Importing.shared.startAutomatic() } }
        .task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(60)) } catch { return }
                backups.schedule()
            }
        }
        .task {
            let updates = CatalogUpdates.shared
            var seen = updates.generation
            while !Task.isCancelled {
                await updates.changed(after: seen)
                do { try await Task.sleep(for: CatalogUpdates.refreshInterval) } catch { return }
                seen = updates.generation
                await library.refreshCatalog()
                clips.reconnect(library.catalogMedia)
            }
        }
        .overlay {
            if let item = selection {
                GeometryReader { geometry in
                    ZStack {
                        Color.black.opacity(0.72).contentShape(Rectangle()).onTapGesture {
                            selection = nil; InteractionLatency.shared.applied("close preview")
                        }
                        Playback(selection: item, annotations: annotations, clips: clips, close: { selection = nil },
                                 position: previewPosition(item, items: displayed), trash: { requestTrash([item.media]) })
                            .id(item.id)
                            .frame(width: max(1, geometry.size.width - 40), height: max(1, geometry.size.height - 40))
                            .background(Color(red: 0.08, green: 0.09, blue: 0.11), in: RoundedRectangle(cornerRadius: 14))
                            .contentShape(Rectangle()).onTapGesture { }
                    }.frame(width: geometry.size.width, height: geometry.size.height)
                }
            }
        }
        .sheet(isPresented: $showImport) { ImportSheet() }
    }
}
