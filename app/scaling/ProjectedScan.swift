import Foundation
import Metal
import Darwin

/// Experimental exhaustive projected-vector scorer; no ANN candidate pruning.
@main struct ProjectedScan {
    static func main() throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let shape = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("projected-shape.json"))) as! [String: Int]
        let full = CommandLine.arguments.contains("--full")
        let rows = shape["rows"]!, dimensions = full ? 768 : shape["dimensions"]!
        let length = rows*dimensions*MemoryLayout<UInt16>.size
        let descriptor = open(root.appendingPathComponent(full ? "full.f16" : "projected.f16").path, O_RDONLY)
        guard descriptor >= 0 else { throw POSIXError(.ENOENT) }
        defer { close(descriptor) }
        let page = Int(getpagesize())
        let mappedLength = ((length+page-1)/page)*page
        let address = mmap(nil, mappedLength, PROT_READ, MAP_PRIVATE, descriptor, 0)!
        guard address != MAP_FAILED else { throw POSIXError(.ENOMEM) }
        defer { munmap(address, mappedLength) }
        let device = MTLCreateSystemDefaultDevice()!
        let weights = device.makeBuffer(bytesNoCopy: address, length: mappedLength, options: .storageModeShared, deallocator: nil)!
        let query = device.makeBuffer(length: dimensions*MemoryLayout<Float>.size, options: .storageModeShared)!
        let scores = device.makeBuffer(length: rows*MemoryLayout<Float>.size, options: .storageModeShared)!
        let library = try device.makeLibrary(source: """
        #include <metal_stdlib>
        using namespace metal;
        kernel void score(device const half *matrix [[buffer(0)]], device const float *query [[buffer(1)]],
                          device float *scores [[buffer(2)]], constant uint &dimensions [[buffer(3)]],
                          uint row [[threadgroup_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
            float result = 0;
            for (uint column=lane; column<dimensions; column+=32)
                result += float(matrix[row*dimensions+column])*query[column];
            result = simd_sum(result);
            if (lane==0) scores[row]=result;
        }
        """, options: nil)
        let pipeline = try device.makeComputePipelineState(function: library.makeFunction(name: "score")!)
        let queue = device.makeCommandQueue()!
        print("{\"ready\":true}");fflush(stdout)
        while let line = readLine() {
            let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
            let values = (request["vector"] as! [NSNumber]).map(\.floatValue)
            guard values.count == dimensions else { throw POSIXError(.EINVAL) }
            values.withUnsafeBytes { query.contents().copyMemory(from: $0.baseAddress!, byteCount: $0.count) }
            var dimensionValue = UInt32(dimensions)
            let started = CFAbsoluteTimeGetCurrent()
            let buffer = queue.makeCommandBuffer()!
            let encoder = buffer.makeComputeCommandEncoder()!
            encoder.setComputePipelineState(pipeline)
            encoder.setBuffer(weights, offset: 0, index: 0)
            encoder.setBuffer(query, offset: 0, index: 1)
            encoder.setBuffer(scores, offset: 0, index: 2)
            encoder.setBytes(&dimensionValue, length: 4, index: 3)
            encoder.dispatchThreadgroups(MTLSize(width: rows, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 32, height: 1, depth: 1))
            encoder.endEncoding();buffer.commit();buffer.waitUntilCompleted()
            if let error = buffer.error { throw error }
            // Return scores through a preallocated mmap-friendly file, avoiding
            // JSON serialization of millions of numbers. Python measures top-k too.
            let data = Data(bytesNoCopy: scores.contents(), count: rows*4, deallocator: .none)
            try data.write(to: root.appendingPathComponent("scores.f32"))
            print("{\"seconds\":\(CFAbsoluteTimeGetCurrent()-started)}");fflush(stdout)
        }
    }
}
