import Foundation
import Vision
import AppKit

// One process handles every sampled frame in a source file. Coordinates are
// normalized to a top-left origin so FFmpeg and the browser can share them.
guard CommandLine.arguments.count >= 2 else {
    fputs("usage: episode-ocr <frames-directory> [language]\n", stderr)
    exit(2)
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let language = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "en-US"
let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil))?
    .filter { $0.pathExtension.lowercased() == "jpg" }
    .sorted { $0.lastPathComponent < $1.lastPathComponent } ?? []
let encoder = JSONSerialization.self
for (index, file) in files.enumerated() {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = [language]
    do {
        try VNImageRequestHandler(url: file, options: [:]).perform([request])
        let lines: [[String: Any]] = (request.results ?? []).compactMap { result in
            guard let candidate = result.topCandidates(1).first else { return nil }
            let box = result.boundingBox
            return ["text": candidate.string, "confidence": candidate.confidence,
                    "x": box.minX, "y": 1 - box.maxY, "w": box.width, "h": box.height]
        }
        let payload: [String: Any] = ["index": index, "lines": lines]
        let data = try encoder.data(withJSONObject: payload, options: [.fragmentsAllowed])
        print(String(data: data, encoding: .utf8)!)
    } catch {
        let payload: [String: Any] = ["index": index, "lines": [], "error": error.localizedDescription]
        let data = try encoder.data(withJSONObject: payload)
        print(String(data: data, encoding: .utf8)!)
    }
}
