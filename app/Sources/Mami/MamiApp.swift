import SwiftUI
import AVKit
import ImageIO
import MamiCore

// Explicit alias selects the property wrapper on SDKs that also export a State macro.
typealias ViewState<Value> = SwiftUI.State<Value>


/// Shutdown belongs to the application, not a window that may already be closed.
final class MamiAppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            Importing.shared.shutdown(); Indexing.shared.stop(); Indexing.previews.stop(); Indexing.faces.stop()
            SearchMaintenance.shared.stop(); ModelDownloads.shared.stop(); CatalogBackups.shared.flush()
        }
    }
}

struct MamiApp: App {
    @NSApplicationDelegateAdaptor(MamiAppDelegate.self) private var delegate
    // One library per process: a second window's models would overwrite the
    // first window's selection and run a second search worker.
    @ViewState private var library = Library()
    @ViewState private var clips = ClipSelection()
    @ViewState private var navigation = BrowserSelection()
    @ViewState private var annotations = Annotations()
    @ViewState private var people = PeopleLibrary()
    var body: some Scene {
        Window("Mami", id: "library") {
            LibraryView(library: library, clips: clips, navigation: navigation, annotations: annotations, people: people)
        }
            .defaultSize(width: 1200, height: 800)
        Settings { MamiSettings() }
    }
}
