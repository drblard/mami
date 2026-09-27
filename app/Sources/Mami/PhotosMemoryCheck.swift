import Foundation
import Darwin

enum PhotosMemoryCheck {
    static func residentBytes() throws -> UInt64 {
        var info = mach_task_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<natural_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { throw AppError.message("Cannot measure resident memory") }
        return info.resident_size
    }

    static func run(file: URL) throws {
        // Deliberately keep the outer pool alive across all reads, as in the
        // background PhotoKit export. Only production hash() can drain chunks.
        try autoreleasepool {
            let expected = try PhotosExporter.hash(file)
            let baseline = try residentBytes()
            var peak = baseline
            for pass in 1...64 {
                guard try PhotosExporter.hash(file) == expected else { throw AppError.message("Read-back digest changed") }
                let current = try residentBytes()
                peak = max(peak, current)
                guard peak <= baseline + 64 * 1024 * 1024 else { throw AppError.message("Verification retained file buffers: \(peak - baseline) bytes") }
                if pass % 8 == 0 { print("HASH MEMORY pass=\(pass) baseline=\(baseline) resident=\(current) peak=\(peak)") }
            }
            print("PHOTOS MEMORY passed 64 repeated production hashes with bounded memory; digest=\(expected)")
        }
    }
}
