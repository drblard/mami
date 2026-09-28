import Foundation
import CoreML

/// Query-only SigLIP tower. The separately versioned artifact is prepared offline.
enum NativeTextEncoder {
    static let tokenCount = 64
    static let vectorDimensions = 768
    struct Request: Decodable { let input_ids: [Int32] }
    struct Reply: Encodable { let embedding: [Float] }

    static func serve(modelURL: URL) throws {
        let configuration = MLModelConfiguration()
        configuration.computeUnits = .cpuOnly
        let model = try MLModel(contentsOf: modelURL, configuration: configuration)
        print("{\"ready\":true}")
        fflush(stdout)
        while let line = readLine() {
            do {
                let request = try JSONDecoder().decode(Request.self, from: Data(line.utf8))
                guard request.input_ids.count == tokenCount, request.input_ids.allSatisfy({ $0 >= 0 && $0 < 256000 }) else {
                    throw AppError.message("Invalid text encoder token sequence")
                }
                let ids = try MLMultiArray(shape: [1, NSNumber(value: tokenCount)], dataType: .int32)
                let mask = try MLMultiArray(shape: [1, NSNumber(value: tokenCount)], dataType: .int32)
                for index in request.input_ids.indices {
                    ids[index] = NSNumber(value: request.input_ids[index])
                    mask[index] = 1
                }
                let response = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: ["input_ids": ids, "attention_mask": mask]))
                guard let vector = response.featureValue(for: "embedding")?.multiArrayValue,
                      vector.count == vectorDimensions else { throw AppError.message("Unexpected text encoder output") }
                let embedding = (0..<vector.count).map { vector[$0].floatValue }
                guard embedding.allSatisfy(\.isFinite) else { throw AppError.message("Nonfinite text embedding") }
                let data = try JSONEncoder().encode(Reply(embedding: embedding))
                print(String(decoding: data, as: UTF8.self))
            } catch {
                let data = try JSONSerialization.data(withJSONObject: ["error": error.localizedDescription])
                print(String(decoding: data, as: UTF8.self))
            }
            fflush(stdout)
        }
    }
}
