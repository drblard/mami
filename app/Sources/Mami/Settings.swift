import SwiftUI
import AppKit

struct MamiSettings: View {
    @ObservedObject private var photos = PhotosImporting.shared
    @ObservedObject private var importing = Importing.shared
    private var busy: Bool { photos.running || importing.photosTransfer }
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
            Section("Archiving") {
                Text("Completed imports are remembered in the backed-up catalog. Changing the destination or moving an imported file does not request another iCloud download.")
                Text("Managed NFS / HDD archiving is not available yet. It will need verified moves and catalog location updates so originals remain accessible when the archive is connected.")
                    .foregroundStyle(.secondary)
            }.font(.caption)
        }.formStyle(.grouped).padding().frame(width: 580, height: 520)
    }
}
