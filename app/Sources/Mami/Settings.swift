import SwiftUI
import AppKit

struct MamiSettings: View {
    @ObservedObject private var photos = PhotosImporting.shared
    @ObservedObject private var importing = Importing.shared
    @ViewState private var reviewCamera = false
    private var busy: Bool { photos.running || importing.photosTransfer }
    private var cameraBusy: Bool { importing.running && !importing.photosTransfer }
    private var transferStatus: String {
        if importing.photosTransfer, let progress = importing.progress {
            return progress.current.isEmpty ? progress.phase : "\(progress.phase) · \(progress.current)"
        }
        if photos.needsPhotosAccess { return "Waiting for Photos access" }
        if photos.error != nil { return "Import needs attention" }
        return photos.running ? "Waiting for completed downloads…" : "Nothing to do right now"
    }
    var body: some View {
        Form {
            Section("iCloud Photos") {
                Toggle("Enable automatic import", isOn: Binding(get: { photos.enabled }, set: {
                    if $0 { photos.enable() } else { photos.disable() }
                })).toggleStyle(.checkbox)
                HStack {
                    VStack(alignment: .leading) {
                        Text("Destination directory")
                        Text(photos.destination.path).font(.caption).textSelection(.enabled)
                    }
                    Spacer()
                    Button("Choose…") {
                        let panel = NSOpenPanel()
                        panel.canChooseFiles = false; panel.canChooseDirectories = true
                        panel.canCreateDirectories = true
                        panel.directoryURL = photos.destination
                        if panel.runModal() == .OK, let url = panel.url { photos.destination = url }
                    }.disabled(busy)
                }
                DatePicker("Import from", selection: $photos.fromDate, in: ...Date(), displayedComponents: .date)
                    .disabled(busy)
                Text("Includes this date and newer photos and videos. Files are organized as year/date inside the chosen directory. Mami never deletes from Photos or iCloud.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Check Photos now") { Task { await photos.scan() } }
                    .disabled(!photos.enabled || busy || importing.running)
                Text(photos.status).font(.caption).lineLimit(2, reservesSpace: true).help(photos.status)
                if photos.needsPhotosAccess {
                    HStack {
                        Button("Allow Photos access…") { photos.enable() }.disabled(busy)
                        Button("Open Photos privacy settings") {
                            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Photos")!)
                        }
                    }
                }
                Text("\(photos.transferred) originals saved and verified this pass")
                    .font(.caption).lineLimit(1)
                Text(transferStatus).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).help(transferStatus)
                if let error = photos.error { Text(error).font(.caption).foregroundStyle(.orange) }
            }
            Section("DJI Camera") {
                Toggle("Automatically offload DJI when connected", isOn: $importing.automaticDJI)
                Toggle("Remove source files after verified offload", isOn: $importing.removeSource)
                    .disabled(cameraBusy)
                Toggle("Include DJI .LRF proxy files", isOn: $importing.includeProxies)
                    .disabled(cameraBusy)
                Toggle("Eject camera after successful offload", isOn: $importing.ejectAfter)
                    .disabled(cameraBusy)
                Text("While Mami is open, your DJI Pocket is detected automatically. Originals are saved in ~/Media/Originals/DJI-Pocket-4P/year/date. Proxies are saved separately in .mami-proxies. Removal always requires a verified independent copy.")
                    .font(.caption).foregroundStyle(.secondary)
                Text(importing.cameraStatus).font(.caption).lineLimit(2, reservesSpace: true)
                if let error = importing.cameraError { Text(error).font(.caption).foregroundStyle(.orange) }
                HStack {
                    Button("Check camera / retry") { importing.retryCamera() }
                    Button("Review files / exceptions…") { reviewCamera = true }
                }.disabled(cameraBusy || importing.listing)
                if importing.running && !importing.photosTransfer {
                    HStack {
                        Button(importing.progress?.paused == true ? "Resume" : "Pause") { importing.togglePause() }
                        Button("Stop offload") { importing.stop() }
                    }
                }
                Text("If disconnected early, reconnect to resume verified work. Failed or stopped offloads do not eject. Turn automatic offload off before connecting to review per-file exceptions first.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Archiving") {
                Text("Completed imports are remembered in the backed-up catalog. Changing the destination or moving an imported file does not request another iCloud download.")
                Text("Managed NFS / HDD archiving is not available yet. It will need verified moves and catalog location updates so originals remain accessible when the archive is connected.")
                    .foregroundStyle(.secondary)
            }.font(.caption)
        }.formStyle(.grouped).padding().frame(width: 620, height: 740)
            .environment(\.locale, Locale(identifier: "en_US"))
            .sheet(isPresented: $reviewCamera) { ImportSheet() }
    }
}
