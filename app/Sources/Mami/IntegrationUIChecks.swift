import SwiftUI
import AVKit
import ImageIO
import MamiCore

/// UI-driving integration checks, launched through `IntegrationCheck`.
enum UIChecks {
    @MainActor static func scanUITest(_ directory: URL) async throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let indexing = Indexing.shared
        let host = NSHostingView(rootView: IndexingBar().padding().frame(width: 1000).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1050, height: 160), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFrontRegardless()
        func until(_ condition: () -> Bool, seconds: Double = 30) async throws {
            let deadline = Date().addingTimeInterval(seconds)
            while !condition() {
                if let error = indexing.error { throw AppError.message(error) }
                guard Date() < deadline else { throw AppError.message("Timed out waiting for scanner: \(indexing.phase)") }
                try await Task.sleep(for: .milliseconds(50))
            }
        }
        func snapshot(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture scan controls") }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent(name), options: .withoutOverwriting)
        }
        Indexing.startAll()
        try await until { indexing.phase == "Checking media" || indexing.phase == "Up to date" || indexing.paused }
        if !indexing.paused { indexing.togglePause() }
        try await until { indexing.paused && indexing.waiting }
        try snapshot("paused.png")
        let count = try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files")
        try await Task.sleep(for: .milliseconds(500))
        guard try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files") == count else {
            throw AppError.message("Scanner continued writing while paused")
        }
        indexing.togglePause()
        try await until { !indexing.paused }
        try snapshot("scanning.png")
        try await until({ indexing.phase == "Up to date" }, seconds: 900)
        let rows = try SQLDatabase(Catalog.standard.database, readOnly: true).scalar("SELECT count(*) FROM scan_files")
        guard rows == "498" else { throw AppError.message("Expected 498 scanned files, got \(rows)") }
        _ = try Catalog.standard.snapshotIfChanged()
        try snapshot("completed.png")
        indexing.stop()
        Indexing.previews.stop()
        window.orderOut(nil)
        print("SCAN UI TEST PASSED: native progress, persisted pause, no writes while paused, resume and 498-file automatic scan")
    }

    @MainActor static func uiTest(_ directory: URL) async throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let library = Library()
        await library.load()
        guard library.ready else { throw AppError.message(library.error ?? "Library not ready") }
        guard library.mode == "both" else { throw AppError.message("Combined search is not the default") }
        guard library.items.allSatisfy({ $0.metadata != nil }) else { throw AppError.message("Missing capture metadata") }
        if !ContentIdentity.locations.isEmpty {
            guard library.items.allSatisfy({ FileManager.default.isReadableFile(atPath: $0.url.path) && $0.assetID.hasPrefix("sha256:") }) else {
                throw AppError.message("Relocated original is unavailable or lost its content identity")
            }
            print("RELOCATION all \(library.items.count) originals readable with content identities")
        }
        let annotationTest = Annotations(directory: directory.appendingPathComponent("annotation-test"))
        await annotationTest.load()
        let firstMedia = library.items[0]
        annotationTest.save(Annotation(favorite: true, tags: ["Test tag"], place: "Test place"), for: firstMedia)
        let restored = try Annotations.read(annotationTest.directory)
        guard restored[firstMedia.assetID]?.favorite == true, restored[firstMedia.assetID]?.tags == ["Test tag"] else {
            throw AppError.message("Annotation persistence failed")
        }
        annotationTest.toggleFavorite(firstMedia)
        let restoredAgain = try Annotations.read(annotationTest.directory)
        guard restoredAgain[firstMedia.assetID]?.favorite == false, restoredAgain[firstMedia.assetID]?.place == "Test place",
               try SQLDatabase(annotationTest.catalog.userDatabase, readOnly: true).scalar("SELECT count(*) FROM annotation_history") == "2" else {
            throw AppError.message("Annotation history was not preserved")
        }
        let selectionCatalog = Catalog(directory: directory.appendingPathComponent("selection-check"))
        let receiptURL = directory.appendingPathComponent("photos-resource-fixture.json")
        let receipt = PhotosExporter.Receipt(file: "no-longer-local.mov", digest: "verified-fixture-digest", size: 123, imported: false)
        try JSONEncoder().encode(receipt).write(to: receiptURL)
        try PhotosExporter.markImported([receiptURL], catalog: selectionCatalog)
        try FileManager.default.removeItem(at: receiptURL)
        guard try selectionCatalog.photosHistory().contains("photos-resource-fixture") else {
            throw AppError.message("Photos import history depended on staging or original location")
        }
        let selectedMedia = Array(library.items.prefix(2))
        try selectionCatalog.synchronize(selectedMedia)
        let clips = ClipSelection(catalog: selectionCatalog)
        await clips.load()
        selectedMedia.forEach { clips.toggle($0) }
        await clips.flush()
        let savedSelection = try selectionCatalog.snapshotIfChanged()
        clips.save(clips.items)
        guard try selectionCatalog.snapshotIfChanged().file == savedSelection.file else { throw AppError.message("Unchanged selection created a backup") }
        let reopened = ClipSelection(catalog: selectionCatalog)
        await reopened.load()
        guard reopened.items == clips.items, reopened.items.count == 2 else { throw AppError.message("Selected clips did not survive reload") }
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("mami-selection-test-\(UUID().uuidString)"))
        let writers = try ClipDragHandle.writers(clips.items)
        guard pasteboard.writeObjects(writers), pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])?.count == 2 else {
            throw AppError.message("Selection did not provide two native file drag items")
        }
        let selectedRestore = Catalog(directory: directory.appendingPathComponent("selection-restored"))
        try FileManager.default.createDirectory(at: selectedRestore.directory, withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: selectionCatalog.backups.appendingPathComponent(savedSelection.file), to: selectedRestore.database)
        guard try selectedRestore.selectedClips() == clips.items else { throw AppError.message("Selection snapshot restore failed") }
        guard try selectedRestore.photosHistory().contains("photos-resource-fixture") else { throw AppError.message("Photos import history backup restore failed") }
        print("PHOTOS durable resource identity survives absent originals, staging removal and catalog restore")
        let pipelineCancellation = PhotosCancellation()
        let pipeline = PhotosPipeline(cancellation: pipelineCancellation)
        let pipelineProducer = Task.detached {
            for index in 0..<20 { try pipeline.enqueue(URL(fileURLWithPath: "/fixture/\(index)"), size: 128 * 1024 * 1024) }
            pipeline.finish()
        }
        var consumed = 0
        while let batch = try await Task.detached(operation: { try pipeline.take() }).value {
            guard batch.count <= 4 else { throw AppError.message("Photos pipeline exceeded byte bound") }
            consumed += batch.count
            pipeline.acknowledge(batch)
        }
        try await pipelineProducer.value
        guard consumed == 20 else { throw AppError.message("Photos pipeline lost work") }
        print("PHOTOS concurrent bounded producer/consumer passed")
        clips.move(clips.items[1].id, by: -1)
        guard clips.items.first?.assetID == selectedMedia[1].assetID else { throw AppError.message("Selection ordering failed") }
        print("SELECTION persistence, no-op backup, snapshot restore, ordering and two native file drag items passed")
        let navigation = BrowserSelection()
        let host = NSHostingView(rootView: LibraryView(library: library, clips: clips, navigation: navigation))
        host.appearance = NSAppearance(named: .darkAqua)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFrontRegardless()
        func snapshot(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw AppError.message("Cannot capture app view") }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { throw AppError.message("Cannot encode app view") }
            try png.write(to: directory.appendingPathComponent(name), options: .withoutOverwriting)
        }
        try await Task.sleep(for: .seconds(1))
        try snapshot("library.png")
        let settingsHost = NSHostingView(rootView: MamiSettings())
        settingsHost.appearance = NSAppearance(named: .darkAqua)
        let settingsWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 520),
                                      styleMask: [.titled], backing: .buffered, defer: false)
        settingsWindow.contentView = settingsHost
        settingsWindow.orderFrontRegardless()
        try await Task.sleep(for: .milliseconds(300))
        settingsHost.layoutSubtreeIfNeeded()
        if let bitmap = settingsHost.bitmapImageRepForCachingDisplay(in: settingsHost.bounds) {
            settingsHost.cacheDisplay(in: settingsHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("settings.png"))
        }
        settingsWindow.orderOut(nil)
        var dateCalendar = Calendar(identifier: .gregorian)
        dateCalendar.timeZone = TimeZone(identifier: "Europe/Bucharest")!
        dateCalendar.firstWeekday = 2
        func filterDay(_ year: Int, _ month: Int, _ day: Int) -> Date {
            dateCalendar.date(from: DateComponents(year: year, month: month, day: day))!
        }
        func checkFilterDates(_ dates: (Date, Date), _ from: Date, _ through: Date) throws {
            guard dates.0 == from, dates.1 == through else { throw AppError.message("Date filter calendar boundary failed") }
        }
        let filterNow = filterDay(2024, 3, 31)
        try checkFilterDates(DateFilterPreset.today.dates(now: filterNow, calendar: dateCalendar), filterNow, filterNow)
        try checkFilterDates(DateFilterPreset.yesterday.dates(now: filterDay(2024, 3, 1), calendar: dateCalendar), filterDay(2024, 2, 29), filterDay(2024, 2, 29))
        try checkFilterDates(DateFilterPreset.seven.dates(now: filterDay(2024, 4, 1), calendar: dateCalendar), filterDay(2024, 3, 26), filterDay(2024, 4, 1))
        try checkFilterDates(DateFilterPreset.thirty.dates(now: filterNow, calendar: dateCalendar), filterDay(2024, 3, 2), filterNow)
        try checkFilterDates(DateFilterPreset.month.dates(now: filterDay(2024, 2, 10), calendar: dateCalendar), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        var draft = DateFilterDraft(from: filterNow, through: filterNow)
        draft.switchMode(.week, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 25), filterNow)
        draft.switchMode(.months, now: filterDay(2024, 2, 10), calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        guard draft.anchor == nil else { throw AppError.message("Default month must not anchor a new range") }
        draft.switchMode(.year, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 1, 1), filterDay(2024, 12, 31))
        draft.switchMode(.day, now: filterNow, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterNow, filterNow)
        draft.select(filterNow, mode: .week, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 25), filterNow)
        draft.select(filterNow, mode: .day, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterNow, filterNow)
        draft.select(filterDay(2024, 2, 10), mode: .months, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 2, 1), filterDay(2024, 2, 29))
        draft.select(filterDay(2023, 12, 10), mode: .months, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2023, 12, 1), filterDay(2024, 2, 29))
        draft.select(filterNow, mode: .year, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 1, 1), filterDay(2024, 12, 31))
        draft.select(filterDay(2024, 4, 5), mode: .range, calendar: dateCalendar)
        draft.select(filterDay(2024, 3, 29), mode: .range, calendar: dateCalendar)
        try checkFilterDates((draft.from, draft.through), filterDay(2024, 3, 29), filterDay(2024, 4, 5))
        let dateHost = NSHostingView(rootView: DateFilterPopover(from: filterDay(2026, 9, 7), through: filterDay(2026, 9, 20), enabled: true, earliest: filterDay(2026, 1, 1), apply: { _, _ in }, clear: {}, cancel: {}))
        dateHost.appearance = NSAppearance(named: .darkAqua)
        let dateWindow = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 740, height: 540), styleMask: [.titled], backing: .buffered, defer: false)
        dateWindow.contentView = dateHost
        dateWindow.makeKeyAndOrderFront(nil)
        try await Task.sleep(for: .milliseconds(250))
        if let bitmap = dateHost.bitmapImageRepForCachingDisplay(in: dateHost.bounds) {
            dateHost.cacheDisplay(in: dateHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("date-filter.png"))
        }
        dateWindow.orderOut(nil)
        print("DATE FILTER presets, leap day, DST, calendar week, single/multiple months, year and reverse range passed")
        let customStart = Date(timeIntervalSince1970: 1_750_000_000)
        let customPredicate = PhotosExporter.fetchOptions(range: DateInterval(start: customStart, end: .distantFuture)).predicate!
        guard customPredicate.evaluate(with: ["creationDate": customStart]),
              customPredicate.evaluate(with: ["creationDate": customStart.addingTimeInterval(400 * 86400)]),
              !customPredicate.evaluate(with: ["creationDate": customStart.addingTimeInterval(-1)]) else {
            throw AppError.message("Configured Photos start date was not inclusive and open-ended")
        }
        let range = PhotosExporter.yearRange()
        guard let newestFirst = PhotosExporter.fetchOptions(range: range).sortDescriptors?.first,
              newestFirst.key == "creationDate", !newestFirst.ascending else {
            throw AppError.message("Photos must fetch newest capture dates first")
        }
        let predicate = PhotosExporter.fetchOptions(range: range).predicate!
        guard predicate.evaluate(with: ["creationDate": range.start]),
              predicate.evaluate(with: ["creationDate": range.end.addingTimeInterval(-1)]),
              !predicate.evaluate(with: ["creationDate": range.start.addingTimeInterval(-1)]),
              !predicate.evaluate(with: ["creationDate": range.end]),
              !predicate.evaluate(with: [:]) else { throw AppError.message("Photos year boundaries failed") }
        guard PreviewSizing.fit(CGSize(width: 4000, height: 2000), into: CGSize(width: 1000, height: 800), scale: 2) == 0.5,
              PreviewSizing.fit(CGSize(width: 100, height: 100), into: CGSize(width: 1000, height: 800), scale: 2) == 1 else {
            throw AppError.message("Preview pixel sizing failed")
        }
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        window.makeFirstResponder(nil)
        navigation.focused = library.items.first { $0.kind == "image" }
        func browserKey(_ code: UInt16, characters: String) async throws {
            let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber,
                                        context: nil, characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code)!
            NSApp.postEvent(event, atStart: false)
            try await Task.sleep(for: .milliseconds(400))
        }
        try await browserKey(49, characters: " ")
        guard navigation.preview?.media.id == navigation.focused?.id, navigation.preview != nil else { throw AppError.message("Space did not open selected preview") }
        try await Task.sleep(for: .seconds(1))
        try snapshot("large-preview.png")
        let previewID = navigation.preview!.media.id
        try await browserKey(123, characters: "\u{f702}")
        if navigation.preview?.media.id == previewID { try await browserKey(124, characters: "\u{f703}") }
        guard navigation.preview?.media.id != previewID else { throw AppError.message("Arrows did not navigate preview") }
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            // Dispatch directly: postEvent substitutes the locked session's
            // hardware cursor location for synthetic mouse coordinates.
            let point = host.convert(NSPoint(x: 12, y: 400), to: nil)
            let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0)!
            app.sendEvent(event)
        }
        try await Task.sleep(for: .milliseconds(300))
        guard navigation.preview == nil else { throw AppError.message("Click outside did not dismiss preview") }
        try await browserKey(49, characters: " ")
        guard navigation.preview != nil else { throw AppError.message("Space did not reopen preview") }
        try await browserKey(49, characters: " ")
        guard navigation.preview == nil, navigation.focused != nil else { throw AppError.message("Space did not close preview and retain selection") }
        print("BROWSING Space toggle, arrow navigation, outside-click dismissal, original-pixel sizing and Photos year-boundary filtering passed")
        navigation.focused = nil
        try await browserKey(124, characters: "\u{f703}")
        let firstGridItem = navigation.focused?.id
        try await browserKey(125, characters: "\u{f701}")
        let belowGridItem = navigation.focused?.id
        guard belowGridItem != firstGridItem else { throw AppError.message("Down did not move to the next grid row") }
        for _ in 0..<navigation.columns { try await browserKey(123, characters: "\u{f702}") }
        guard navigation.focused?.id == firstGridItem else { throw AppError.message("Down moved by the wrong number of columns") }
        for _ in 0..<navigation.columns { try await browserKey(124, characters: "\u{f703}") }
        guard navigation.focused?.id == belowGridItem else { throw AppError.message("Grid row movement was inconsistent") }
        try await browserKey(126, characters: "\u{f700}")
        guard navigation.focused?.id == firstGridItem,
              GridNavigation.target(index: 1, key: 126, count: 8, columns: 3) == 1,
              GridNavigation.target(index: 5, key: 125, count: 8, columns: 3) == 7,
              GridNavigation.target(index: 6, key: 125, count: 8, columns: 3) == 6 else {
            throw AppError.message("Grid navigation boundaries failed")
        }
        print("GRID up/down move by \(navigation.columns) columns; first and partial last rows passed")
        let picks = Array(library.items.prefix(3))
        navigation.select(picks[0], extending: false)
        navigation.select(picks[2], extending: true)
        navigation.select(picks[1], extending: true)
        navigation.select(picks[1], extending: true)
        guard navigation.selectedIDs == Set([picks[0].id, picks[2].id]) else { throw AppError.message("Command-selection toggle failed") }
        try await browserKey(49, characters: " ")
        guard let selectedPreview = navigation.previewItems, selectedPreview.count == 2 else { throw AppError.message("Multiple selection did not scope preview") }
        let firstSelected = selectedPreview[0], lastSelected = selectedPreview[1]
        try await browserKey(123, characters: "\u{f702}")
        guard navigation.preview?.media.id == firstSelected.id else { throw AppError.message("Left did not stay within selected preview") }
        let firstPreviewID = navigation.preview?.id
        try await browserKey(123, characters: "\u{f702}")
        guard navigation.preview?.id == firstPreviewID else { throw AppError.message("Preview reloaded at first boundary") }
        try await browserKey(125, characters: "\u{f701}")
        guard navigation.preview?.media.id == firstSelected.id else { throw AppError.message("Down navigated multi-preview") }
        try await browserKey(124, characters: "\u{f703}")
        guard navigation.preview?.media.id == lastSelected.id else { throw AppError.message("Right did not stay within selected preview") }
        let lastPreviewID = navigation.preview?.id
        try await browserKey(124, characters: "\u{f703}")
        guard navigation.preview?.id == lastPreviewID else { throw AppError.message("Preview reloaded at last boundary") }
        try await browserKey(49, characters: " ")
        clips.clear()
        try await browserKey(11, characters: "b")
        guard Set(clips.items.map(\.assetID)) == Set([picks[0].assetID, picks[2].assetID]) else { throw AppError.message("B did not add multi-selection") }
        try await browserKey(11, characters: "b")
        guard clips.items.isEmpty else { throw AppError.message("B did not remove selected clips") }
        navigation.select(picks[0], extending: false)
        navigation.select(picks[0], extending: true)
        guard navigation.selectedIDs.isEmpty, navigation.focused == nil else { throw AppError.message("Deselecting final item left a selection") }
        navigation.click(picks[0], extending: false)
        navigation.click(picks[0], extending: false)
        guard navigation.selectedIDs.isEmpty, navigation.focused == nil else { throw AppError.message("Second click did not deselect") }
        navigation.select(picks[0], extending: false)
        navigation.select(picks[0], extending: false)
        guard navigation.selectedIDs == [picks[0].id] else { throw AppError.message("Keyboard focus toggled the selection") }
        clips.add([picks[0], picks[1]])
        clips.add([picks[1], picks[2], picks[2]])
        guard clips.items.map(\.assetID) == picks.map(\.assetID) else { throw AppError.message("Dropped media were duplicated or reordered") }
        clips.clear()
        print("CLICK second click deselects; keyboard focus does not; dropped clips append without duplicates")
        print("MULTISELECT additive toggle, selection-only left/right preview, ignored up/down and bulk B shortcut passed")
        guard navigation.itemsForDrag(picks[0], in: picks).map(\.id) == [picks[0].id] else { throw AppError.message("Unselected drag did not select item") }
        navigation.select(picks[2], extending: true)
        guard navigation.itemsForDrag(picks[0], in: picks).map(\.id) == [picks[0].id, picks[2].id] else { throw AppError.message("Selected drag lost multi-selection") }
        guard navigation.itemsForDrag(picks[1], in: picks).map(\.id) == [picks[1].id], navigation.selectedIDs == [picks[1].id] else { throw AppError.message("New-item drag did not replace selection") }
        let boundedCache = FrameCache()
        for size in 64..<330 { _ = await boundedCache.image(picks[0].match.frame, maxPixelSize: size) }
        guard await boundedCache.retainedCount <= 256, await boundedCache.retainedCost <= 96 * 1024 * 1024 else { throw AppError.message("Thumbnail cache exceeded limits") }
        print("GRID DRAG selection replacement/multiple originals and bounded thumbnail cache passed")
        // Exercise actual AppKit card rectangles and mouse-down routing, not just
        // the selection helper: A selected, press/drag B, even after a stale A press.
        let dragRouter = MediaDragRouter()
        let dragA = library.items.first { $0.kind == "image" }!
        let dragB = library.items.first { $0.kind == "video" }!
        let dragMedia = [dragA, dragB]
        let dragWindow = NSWindow(contentRect: NSRect(x: 50, y: 50, width: 500, height: 240), styleMask: [.titled], backing: .buffered, defer: false)
        let dragRoot = NSView(frame: NSRect(x: 0, y: 0, width: 500, height: 240))
        let cardA = MediaDragSurface.DragView(frame: NSRect(x: 10, y: 10, width: 200, height: 200))
        let cardB = MediaDragSurface.DragView(frame: NSRect(x: 250, y: 10, width: 200, height: 200))
        cardA.items = { navigation.itemsForDrag(dragA, in: dragMedia) }
        cardB.items = { navigation.itemsForDrag(dragB, in: dragMedia) }
        dragRoot.addSubview(cardA); dragRoot.addSubview(cardB)
        dragWindow.contentView = dragRoot; dragWindow.makeKeyAndOrderFront(nil)
        dragRouter.register(cardA); dragRouter.register(cardB)
        func dragEvent(_ type: NSEvent.EventType, _ x: CGFloat) -> NSEvent {
            NSEvent.mouseEvent(with: type, location: NSPoint(x: x, y: 80), modifierFlags: [], timestamp: 0,
                               windowNumber: dragWindow.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
        }
        navigation.select(dragA, extending: false)
        dragRouter.mouseDown(dragEvent(.leftMouseDown, 100))
        dragRouter.mouseDown(dragEvent(.leftMouseDown, 330))
        // A SwiftUI update must not replace the provider captured on mouse-down.
        cardB.items = { [dragA] }
        guard let routed = dragRouter.dragItems(dragEvent(.leftMouseDragged, 350)), routed.0 === cardB,
              routed.1.map(\.url) == [dragB.url], navigation.selectedIDs == [dragB.id],
              dragRouter.dragItems(dragEvent(.leftMouseDragged, 370)) == nil else {
            throw AppError.message("Mouse-down on B dragged stale selection A")
        }
        dragWindow.orderOut(nil); window.makeKeyAndOrderFront(nil)
        print("GRID DRAG AppKit mouse routing: stale A press, B press/drag, frozen provider, single session passed")
        let september = try DateSearch.parse("goats in September 2025")
        let leap = try DateSearch.parse("in February 2024")
        let exact = try DateSearch.parse("goats from 2026-01-01 to 2026-01-31")
        guard september.text == "goats", september.range == CaptureRange(from: "2025-09-01", through: "2025-09-30"),
              leap.text.isEmpty, leap.range?.through == "2024-02-29",
              exact.range?.contains("20260131235959") == true, exact.range?.contains("20260201000000") == false,
              exact.range?.contains(nil) == false,
              try DateSearch.parse("goats in September").range?.from == "\(Calendar.current.component(.year, from: Date()))-09-01" else {
            throw AppError.message("Natural-language date parsing or inclusive boundaries failed")
        }
        do {
            _ = try DateSearch.parse("from 2026-02-30 to 2026-03-01")
            throw AppError.message("Invalid date was accepted")
        } catch let error as AppError {
            if error.localizedDescription == "Invalid date was accepted" { throw error }
        }
        if let date = library.catalogMedia.compactMap({ $0.metadata?.sortDate }).first(where: { $0.count >= 8 }) {
            let day = "\(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.dropFirst(6).prefix(2))"
            library.query = "goats on \(day)"
            await library.searchAndWait()
            let count = library.catalogMedia.filter { library.matchesDate($0) }.count
            guard library.items.count == min(60, count), library.items.allSatisfy({ library.matchesDate($0) }) else { throw AppError.message("Date range was not applied before search limit") }
            library.query = "on \(day)"
            await library.searchAndWait()
            guard library.items.count == count, !library.showingMatches else { throw AppError.message("Date-only search failed") }
            library.query = ""; await library.searchAndWait()
        }
        print("DATES month/year, leap year, inclusive range, unknown dates, invalid dates and pre-limit search filtering passed")
        guard MediaFormat.classify(width: 1920, height: 1080, orientation: 6) == .vertical,
              MediaFormat.classify(width: 1080, height: 1920) == .vertical,
              MediaFormat.classify(width: 1080, height: 1080) == .square,
              MediaFormat.classify(width: 0, height: 1080) == .unknown,
              !library.formats.values.contains(.unknown) else { throw AppError.message("Display shape classification failed") }
        print("SHAPES \(Dictionary(grouping: library.formats.values, by: { $0.rawValue }).mapValues(\.count))")
        for format in [MediaFormat.vertical, .horizontal, .square] {
            library.format = format
            library.query = "goats"
            // Let the view's automatic filter-change search settle before awaiting this query.
            try await Task.sleep(for: .milliseconds(100))
            await library.searchAndWait()
            let available = library.formats.values.filter { $0 == format }.count
            guard library.items.count == min(60, available), library.items.allSatisfy({ library.matchesFormat($0) }) else {
                throw AppError.message("Shape filter failed for \(format): \(library.items.count) of \(available)")
            }
            try await Task.sleep(for: .milliseconds(200))
            try snapshot("shape-\(format.rawValue).png")
        }
        window.setContentSize(NSSize(width: 850, height: 650))
        try await Task.sleep(for: .milliseconds(200))
        try snapshot("compact-filters.png")
        window.setContentSize(NSSize(width: 1200, height: 800))
        library.format = .all
        if let device = library.devices.first {
            library.deviceFilter = device
            try await Task.sleep(for: .milliseconds(100))
            await library.searchAndWait()
            let count = library.catalogMedia.filter { $0.device == device }.count
            guard library.items.count == min(60, count), library.items.allSatisfy({ $0.device == device }) else {
                throw AppError.message("Device filter did not constrain search candidates")
            }
            try snapshot("device-filter.png")
            library.deviceFilter = "All devices"
            print("DEVICE filter passed: \(device), \(count) available originals")
        }
        try await Task.sleep(for: .milliseconds(100))
        library.query = "bringing food to goats"
        await library.searchAndWait()
        guard library.items.count == 60, library.showingMatches else { throw AppError.message("UI search failed") }
        try await Task.sleep(for: .seconds(1))
        try snapshot("search.png")
        if library.speechAvailable {
            library.query = "Dunăre"
            await library.searchAndWait()
            guard library.items.count == 60, library.items.contains(where: { $0.match.evidence != nil }),
                  Set(library.items.map(\.path)).count == library.items.count else {
                throw AppError.message("Combined search lost spoken matches or duplicated files")
            }
            try await Task.sleep(for: .milliseconds(300))
            try snapshot("both.png")
            print("BOTH combined search includes speech evidence and distinct files")
            library.mode = "speech"
            try await Task.sleep(for: .milliseconds(200))
            library.query = "Dunăre"
            await library.searchAndWait()
            guard !library.items.isEmpty, library.items.allSatisfy({ $0.match.evidence != nil }) else {
                throw AppError.message("Spoken-word search failed")
            }
            try await Task.sleep(for: .seconds(1))
            try snapshot("speech.png")
            print("SPEECH_MATCHES \(library.items.count)")
        }
        library.query = ""
        await library.searchAndWait()
        guard library.items.count == 498, !library.showingMatches else { throw AppError.message("Clear search failed") }
        await library.worker.stop()
        var cameraConnections = CameraConnections()
        guard Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 3),
              !Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 60),
              !Indexing.activeEditing(bundleID: "com.lemon.lvoverseas", idleSeconds: 600),
              !Indexing.activeEditing(bundleID: "local.mami.prototype", idleSeconds: 0),
              !Indexing.activeEditing(bundleID: nil, idleSeconds: 0) else {
            throw AppError.message("CapCut activity must require foreground and recent input")
        }
        print("EDITOR foreground/recent input yields; idle, background and Mami activity do not")
        let cameraFixture = URL(fileURLWithPath: "/Volumes/DJI-Test")
        guard cameraConnections.next([cameraFixture], enabled: false, busy: false) == nil,
              cameraConnections.next([cameraFixture], enabled: true, busy: true) == nil,
              cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture,
              cameraConnections.next([cameraFixture], enabled: true, busy: false) == nil else {
            throw AppError.message("Camera enable/busy/once-per-connection scheduling failed")
        }
        cameraConnections.disconnected(cameraFixture)
        guard cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture else {
            throw AppError.message("Camera reconnect retry failed")
        }
        cameraConnections.retry()
        guard cameraConnections.next([cameraFixture], enabled: true, busy: false) == cameraFixture else {
            throw AppError.message("Camera explicit retry failed")
        }
        print("CAMERA automatic enable, busy deferral, once-per-connection, reconnect and explicit retry passed")
        if let source = ProcessInfo.processInfo.environment["MAMI_IMPORT_TEST_SOURCE"],
           ProcessInfo.processInfo.environment["MAMI_IMPORT_TEST_DESTINATION"] != nil {
            let importer = Importing.shared
            importer.source = URL(fileURLWithPath: source)
            importer.device = "Import-Fixture"
            let importHost = NSHostingView(rootView: ImportSheet())
            importHost.appearance = NSAppearance(named: .darkAqua)
            window.contentView = importHost
            importer.start()
            let deadline = Date().addingTimeInterval(90)
            while importer.running && Date() < deadline { try await Task.sleep(for: .milliseconds(50)) }
            guard !importer.running, importer.error == nil, importer.progress?.phase == "Import complete",
                  importer.progress?.copied == 1, importer.progress?.duplicates == 1,
                  importer.progress?.failed == 0 else { throw AppError.message("Native import failed: \(importer.error ?? importer.progress?.phase ?? "No progress")") }
            try await Task.sleep(for: .milliseconds(300))
            importHost.layoutSubtreeIfNeeded()
            if let view = window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: bitmap)
                try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("import-complete.png"), options: .withoutOverwriting)
            }
            print("IMPORT native controller verified one new copy and one existing catalog duplicate")
        }
        window.orderOut(nil)
        if ProcessInfo.processInfo.environment["MAMI_PHOTOS_TRANSFER_TEST"] == "1" {
            let incoming = directory.appendingPathComponent("photos-fixture/incoming")
            let receiptFolder = incoming.appendingPathComponent(".receipts")
            let destination = directory.appendingPathComponent("photos-fixture/originals")
            try FileManager.default.createDirectory(at: receiptFolder, withIntermediateDirectories: true)
            let original = library.items.first { $0.kind == "image" }!
            let staged = incoming.appendingPathComponent(original.url.lastPathComponent)
            try FileManager.default.copyItem(at: original.url, to: staged)
            let digest = try await Task.detached { try PhotosExporter.hash(staged) }.value
            let size = (try FileManager.default.attributesOfItem(atPath: staged.path)[.size] as! NSNumber).int64Value
            let receiptURL = receiptFolder.appendingPathComponent("native-" + UUID().uuidString + ".json")
            try JSONEncoder().encode(PhotosExporter.Receipt(file: staged.lastPathComponent, digest: digest, size: size, imported: false, captureDate: Date()))
                .write(to: receiptURL)
            _ = try Catalog.standard.photosHistory()
            try await Importing.shared.importPhotosFolder(incoming, destination: destination, receipts: [receiptURL])
            let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: receiptURL)) as! [String: Any]
            guard let path = saved["destination"] as? String,
                  try PhotosExporter.hash(URL(fileURLWithPath: path)) == digest,
                  !FileManager.default.fileExists(atPath: staged.path),
                  try Catalog.standard.photosHistory().contains(receiptURL.deletingPathExtension().lastPathComponent) else {
                throw AppError.message("Native Photos batch verification/history/cleanup failed")
            }
            print("PHOTOS native controller copied real media, verified history, removed staging and preserved final bytes")
        }
        let video = library.items.first { $0.kind == "video" }!
        let player = try await preparedPlayer(url: video.url, timestamp: 5)
        let transport = PlaybackTransport()
        transport.muted = true
        transport.attach(player)
        guard abs(transport.position - 5) < 0.2, transport.duration > 5 else {
            throw AppError.message("Initial transport position was not populated")
        }
        await transport.seek(to: 10, resume: false)?.value
        guard abs(transport.position - 10) < 0.2 else {
            throw AppError.message("Transport position failed to refresh after seeking")
        }
        let playbackHost = NSHostingView(rootView: VStack {
            VideoSurface(player: transport.player)
            TransportBar(transport: transport)
        }.padding().preferredColorScheme(.dark))
        window.contentView = playbackHost
        window.orderFrontRegardless()
        try await Task.sleep(for: .milliseconds(300))
        func key(_ characters: String, code: UInt16) throws {
            guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                                              windowNumber: window.windowNumber, context: nil, characters: characters,
                                              charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code),
                  window.performKeyEquivalent(with: event) else { throw AppError.message("Transport shortcut was not handled: \(code)") }
        }
        try key("\u{f702}", code: 123)
        try await Task.sleep(for: .milliseconds(400))
        guard abs(transport.position - 5) < 0.2 else { throw AppError.message("Left-arrow seek failed") }
        try key("\u{f703}", code: 124)
        try await Task.sleep(for: .milliseconds(400))
        guard abs(transport.position - 10) < 0.2 else { throw AppError.message("Right-arrow seek failed") }
        try key("m", code: 46)
        guard !transport.muted else { throw AppError.message("Mute shortcut failed") }
        try key("m", code: 46)
        guard transport.muted else { throw AppError.message("Mute shortcut did not toggle back") }
        print("TRANSPORT left/right seek and mute keyboard shortcuts passed")
        if CommandLine.arguments.contains("--verify-playing") {
            try key(" ", code: 49)
            try await Task.sleep(for: .seconds(2))
            guard transport.position > 10.5, abs(transport.position - player.currentTime().seconds) < 0.4 else {
                throw AppError.message("Transport did not follow playback without hover: \(transport.position), player \(player.currentTime().seconds)")
            }
            try key(" ", code: 49)
            let paused = transport.position
            try await Task.sleep(for: .milliseconds(500))
            guard abs(transport.position - paused) < 0.25, !transport.playing else {
                throw AppError.message("Transport did not pause")
            }
            transport.scrub(true)
            transport.position = 5
            transport.scrub(false)
            try await Task.sleep(for: .milliseconds(700))
            guard abs(transport.position - 5) < 0.2, !transport.playing else {
                throw AppError.message("Paused scrubbing did not remain paused")
            }
            transport.toggle()
            try await Task.sleep(for: .milliseconds(300))
            transport.scrub(true)
            transport.position = 10
            transport.scrub(false)
            try await Task.sleep(for: .seconds(2))
            guard transport.position > 10.5, transport.playing else {
                throw AppError.message("Playing scrub did not resume")
            }
            transport.toggle()
            print("TRANSPORT continuous updates, pause, paused scrub and playing scrub passed")
        }
        playbackHost.layoutSubtreeIfNeeded()
        if let bitmap = playbackHost.bitmapImageRepForCachingDisplay(in: playbackHost.bounds) {
            playbackHost.cacheDisplay(in: playbackHost.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("transport.png"), options: .withoutOverwriting)
        }
        transport.stop()
        window.orderOut(nil)
        print("TRANSPORT initial position and seek refresh passed without hover")
        library.query = ""; library.format = .all; library.deviceFilter = "All devices"; library.dateEnabled = false
        await library.searchAndWait()
        library.gridLocked = true
        let before = library.items.map(\.id)
        let original = library.catalogMedia.first { $0.kind == "image" }!
        let arrivalURL = directory.appendingPathComponent("grid-arrival." + original.url.pathExtension)
        try FileManager.default.copyItem(at: original.url, to: arrivalURL)
        let arrival = Media(path: "grid-arrival", kind: original.kind, url: arrivalURL, frames: original.frames,
                            match: original.match, metadata: original.metadata, assetID: "fixture:grid-arrival")
        try Catalog.standard.synchronize([arrival])
        await library.refreshCatalog()
        guard library.items.map(\.id) == before, library.pendingMediaCount == 1 else { throw AppError.message("Locked grid changed during catalog refresh") }
        await library.searchAndWait()
        guard library.items.map(\.id) == before else { throw AppError.message("Search escaped locked catalog snapshot") }
        library.refreshGrid()
        guard library.gridLocked, library.pendingMediaCount == 0, library.items.contains(where: { $0.id == arrival.id }) else {
            throw AppError.message("Explicit refresh did not include new media while retaining lock")
        }
        library.gridLocked = false
        print("GRID LOCK stable snapshot, one pending arrival, explicit refresh and unlock passed")
        print("UI TEST PASSED: \(directory.path)")
    }

    @MainActor static func selfTest() async throws {
        let config = try Configuration.load()
        let media = try Library.readMedia(config.index)
        guard media.count == 498 else { throw AppError.message("Expected 498 files, got \(media.count)") }
        let frames = media.filter { $0.kind == "video" }.prefix(12).flatMap { Array($0.frames.prefix(12)) }
        var cold: [Double] = [], warm: [Double] = []
        for frame in frames {
            let start = CFAbsoluteTimeGetCurrent()
            guard await FrameCache.shared.image(frame.frame, crop: frame.crop) != nil else { throw AppError.message("Missing cached frame \(frame.frame)") }
            cold.append((CFAbsoluteTimeGetCurrent() - start) * 1000)
        }
        for frame in frames {
            let start = CFAbsoluteTimeGetCurrent()
            _ = await FrameCache.shared.image(frame.frame, crop: frame.crop)
            warm.append((CFAbsoluteTimeGetCurrent() - start) * 1000)
        }
        func percentile(_ values: [Double]) -> Double { values.sorted()[min(values.count - 1, Int(Double(values.count) * 0.95))] }
        print("FILES \(media.count) FRAMES \(media.reduce(0) { $0 + $1.frames.count })")
        print("THUMBNAIL_MS cold_p95=\(percentile(cold)) warm_p95=\(percentile(warm)) samples=\(frames.count)")
        for item in media.filter({ $0.kind == "video" }).prefix(3) {
            let asset = AVURLAsset(url: item.url)
            let duration = try await asset.load(.duration).seconds
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 960, height: 540)
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
            let start = CFAbsoluteTimeGetCurrent()
            _ = try await generator.image(at: CMTime(seconds: duration / 2, preferredTimescale: 600))
            print("ORIGINAL_SEEK_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000) \(item.path)")
        }
        if let video = media.first(where: { $0.kind == "video" }) {
            let start = CFAbsoluteTimeGetCurrent()
            let player = try await preparedPlayer(url: video.url, timestamp: 5)
            player.isMuted = true
            let videoView = AVPlayerView(frame: NSRect(x: 0, y: 0, width: 640, height: 360))
            videoView.player = player
            let playbackWindow = NSWindow(contentRect: videoView.frame, styleMask: [.titled], backing: .buffered, defer: false)
            playbackWindow.contentView = videoView
            playbackWindow.orderFrontRegardless()
            print("PLAYER_SEEK_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000)")
            player.play()
            try await Task.sleep(for: .seconds(2))
            let position = player.currentTime().seconds
            print("PLAYER_STATE position=\(position) rate=\(player.rate) control=\(player.timeControlStatus.rawValue) reason=\(String(describing: player.reasonForWaitingToPlay)) error=\(String(describing: player.error))")
            player.pause()
            player.replaceCurrentItem(with: nil)
            playbackWindow.orderOut(nil)
            guard position > 5.2 else { throw AppError.message("AVPlayer did not advance after seek") }
            print("PLAYER_ADVANCED_TO \(position)")
        }
        let worker = SearchWorker()
        do {
            try await worker.start(config)
            for query in ["bringing food to goats", "milking goats", "picking plums"] {
                let start = CFAbsoluteTimeGetCurrent()
                let reply = try await worker.search(query)
                guard let hits = reply.hits, hits.count == 60, Set(hits.map(\.path)).count == hits.count,
                      hits.allSatisfy({ hit in media.contains { $0.path == hit.path } }) else {
                    throw AppError.message("Search returned invalid results")
                }
                print("SEARCH_MS \((CFAbsoluteTimeGetCurrent() - start) * 1000) \(query)")
            }
            await worker.stop()
        } catch { await worker.stop(); throw error }
        print("SELF-TEST PASSED")
    }
}
