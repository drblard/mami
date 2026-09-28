import Foundation
import CoreML

let started = CFAbsoluteTimeGetCurrent()
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let units = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "gpu"
let config = MLModelConfiguration()
config.computeUnits = units == "cpu" ? .cpuOnly : units == "ne" ? .cpuAndNeuralEngine : .cpuAndGPU
let tokens = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("query-tokens.json"))) as! [String: Any]
let ids = tokens["input_ids"] as! [[Int]]
let masks = tokens["attention_mask"] as! [[Int]]
let loadStarted = CFAbsoluteTimeGetCurrent()
let model = try MLModel(contentsOf: root.appendingPathComponent("SiglipText.mlmodelc"), configuration: config)
let loaded = CFAbsoluteTimeGetCurrent()
var timings: [Double] = []
var embeddings: [[Double]] = []
for index in ids.indices {
    let before = CFAbsoluteTimeGetCurrent()
    let input = try MLMultiArray(shape: [1, NSNumber(value: ids[index].count)], dataType: .int32)
    let mask = try MLMultiArray(shape: [1, NSNumber(value: masks[index].count)], dataType: .int32)
    for position in ids[index].indices {
        input[position] = NSNumber(value: ids[index][position])
        mask[position] = NSNumber(value: masks[index][position])
    }
    let result = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: ["input_ids": input, "attention_mask": mask]))
    let embedding = result.featureValue(for: "embedding")!.multiArrayValue!
    embeddings.append((0..<embedding.count).map { embedding[$0].doubleValue })
    timings.append((CFAbsoluteTimeGetCurrent()-before)*1000)
}
let sorted = timings.sorted()
let report: [String: Any] = ["units": units, "model_load_ms": (loaded-loadStarted)*1000,
                           "ready_ms": (loaded-started)*1000, "first_query_ms": timings[0],
                           "p50_ms": sorted[sorted.count/2], "p95_ms": sorted[Int(Double(sorted.count)*0.95)],
                           "timings_ms": timings, "embeddings": embeddings,
                           "caveat": "Fresh native process, precompiled model; OS cache may be warm. Tokenization excluded."]
let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
try data.write(to: root.appendingPathComponent("native-encoder-\(units)-\(UUID().uuidString).json"), options: .withoutOverwriting)
let summary = report.filter { $0.key != "embeddings" && $0.key != "timings_ms" }
print(String(data: try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys]), encoding: .utf8)!)
