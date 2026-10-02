"""Face detection, alignment and identity embeddings (SCRFD + ArcFace, ONNX Runtime).

Pure array functions are separate from model sessions so decoding, alignment and
quality measures are testable without model files.
"""
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from model_config import verify_file

DETECTION_SCORE_THRESHOLD = 0.5
DETECTION_NMS_THRESHOLD = 0.4
DETECTOR_STRIDES = (8, 16, 32)
DETECTOR_ANCHORS_PER_CELL = 2
DETECTOR_MEAN, DETECTOR_STD = 127.5, 128.0
RECOGNIZER_MEAN, RECOGNIZER_STD = 127.5, 127.5
ALIGNED_SIZE = 112
EMBEDDING_DIMENSIONS = 512
# Network input sides must be multiples of the largest stride.
INPUT_ALIGNMENT = 32
RECOGNIZER_BATCH = 32
# Standard ArcFace 112x112 template: eyes, nose tip, mouth corners.
ARCFACE_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                             [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)


@dataclass(frozen=True)
class Face:
    box: tuple  # x1, y1, x2, y2 in source pixels
    keypoints: tuple  # five (x, y) pairs in source pixels
    score: float


def letterbox_scale(width, height, side):
    """Scale that fits an image into a fixed square detector input without upscaling."""
    if width <= 0 or height <= 0 or side < INPUT_ALIGNMENT or side % INPUT_ALIGNMENT:
        raise ValueError('Image sizes must be positive and detector sides stride-aligned')
    return min(1.0, side / max(width, height))


def static_detector_name(side):
    return f'scrfd_10g_bnkps-{side}.onnx'


def prepare_static_detector(source, target, side):
    """Fix the detector's input to side x side and re-infer outputs for CoreML.

    The published model declares 640x640 output shapes despite dynamic inputs,
    which CoreML's static compilation rejects for any other size.
    """
    import onnx
    from onnx import shape_inference
    model = onnx.load(str(source))
    dimensions = model.graph.input[0].type.tensor_type.shape.dim
    if len(dimensions) != 4:
        raise ValueError('Unexpected detector input rank')
    for dimension in dimensions[2:]:
        dimension.ClearField('dim_param'); dimension.dim_value = side
    for output in model.graph.output:
        output.type.tensor_type.ClearField('shape')
    del model.graph.value_info[:]
    model = shape_inference.infer_shapes(model)
    target = Path(target)
    temporary = target.with_name(target.name + '.partial')
    onnx.save(model, str(temporary))
    temporary.replace(target)


def anchor_centers(height, width, stride):
    rows, columns = np.mgrid[:height, :width]
    centers = np.stack([columns, rows], axis=-1).reshape(-1, 2).astype(np.float32) * stride
    return np.repeat(centers, DETECTOR_ANCHORS_PER_CELL, axis=0)


def decode(outputs, network_width, network_height, scale, threshold=DETECTION_SCORE_THRESHOLD):
    """SCRFD outputs: scores, box distances and keypoint distances per stride."""
    count = len(DETECTOR_STRIDES)
    if len(outputs) != count * 3:
        raise ValueError(f'Expected {count * 3} detector outputs, got {len(outputs)}')
    scores, boxes, points = [], [], []
    for index, stride in enumerate(DETECTOR_STRIDES):
        score = np.asarray(outputs[index]).reshape(-1)
        distances = np.asarray(outputs[index + count]).reshape(-1, 4) * stride
        keypoints = np.asarray(outputs[index + count * 2]).reshape(-1, 10) * stride
        centers = anchor_centers(network_height // stride, network_width // stride, stride)
        if not (len(score) == len(distances) == len(keypoints) == len(centers)):
            raise ValueError('Detector output shape does not match its input size')
        keep = score >= threshold
        centers = centers[keep]
        scores.append(score[keep])
        boxes.append(np.concatenate([centers - distances[keep, :2], centers + distances[keep, 2:]], axis=1))
        points.append(np.tile(centers, 5) + keypoints[keep])
    scores = np.concatenate(scores)
    boxes = np.concatenate(boxes) / scale
    points = np.concatenate(points) / scale
    order = non_maximum_suppression(boxes, scores)
    return [Face(tuple(float(v) for v in boxes[i]), tuple(tuple(float(v) for v in pair) for pair in points[i].reshape(5, 2)),
                 float(scores[i])) for i in order]


def non_maximum_suppression(boxes, scores, threshold=DETECTION_NMS_THRESHOLD):
    order = list(np.argsort(-scores, kind='stable'))
    keep = []
    while order:
        best = order.pop(0)
        keep.append(best)
        if not order:
            break
        rest = np.array(order)
        x1 = np.maximum(boxes[best, 0], boxes[rest, 0]); y1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[best, 2], boxes[rest, 2]); y2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        intersection = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        area = lambda b: np.clip(b[..., 2] - b[..., 0], 0, None) * np.clip(b[..., 3] - b[..., 1], 0, None)
        union = area(boxes[best]) + area(boxes[rest]) - intersection
        overlap = np.divide(intersection, union, out=np.zeros_like(union), where=union > 0)
        order = [index for index, value in zip(order, overlap) if value <= threshold]
    return keep


def merge_scales(detections):
    """Union detections from several input scales (source pixels), keeping the best of overlaps.

    Large faces exceed the detector's anchors at high resolution, while small
    faces vanish at low resolution; each scale covers the other's blind spot.
    """
    faces = [face for scale in detections for face in scale]
    if not faces:
        return []
    boxes = np.array([face.box for face in faces], dtype=np.float64)
    scores = np.array([face.score for face in faces], dtype=np.float64)
    return [faces[index] for index in non_maximum_suppression(boxes, scores)]


def similarity_transform(source, target):
    """Least-squares rotation, uniform scale and translation (Umeyama, no reflection)."""
    source = np.asarray(source, dtype=np.float64); target = np.asarray(target, dtype=np.float64)
    source_mean, target_mean = source.mean(axis=0), target.mean(axis=0)
    source_centered, target_centered = source - source_mean, target - target_mean
    variance = (source_centered ** 2).sum() / len(source)
    if variance <= 0:
        raise ValueError('Keypoints are degenerate')
    covariance = target_centered.T @ source_centered / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    sign = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[-1] = -1
    rotation = u @ np.diag(sign) @ vt
    scale = (singular * sign).sum() / variance
    matrix = np.zeros((2, 3))
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = target_mean - matrix[:, :2] @ source_mean
    return matrix


def align(image, keypoints):
    """Warp an RGB PIL image to the 112x112 ArcFace crop; returns uint8 HxWx3."""
    from PIL import Image
    matrix = similarity_transform(keypoints, ARCFACE_TEMPLATE)
    inverse = np.linalg.inv(np.vstack([matrix, [0, 0, 1]]))[:2]
    warped = image.transform((ALIGNED_SIZE, ALIGNED_SIZE), Image.Transform.AFFINE, tuple(inverse.reshape(-1)),
                             resample=Image.Resampling.BILINEAR, fillcolor=(0, 0, 0))
    return np.asarray(warped, dtype=np.uint8)


def sharpness(aligned):
    """Variance of a 4-neighbour Laplacian over the aligned crop's luminance."""
    gray = np.asarray(aligned, dtype=np.float64) @ np.array([0.299, 0.587, 0.114])
    laplacian = (gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4 * gray[1:-1, 1:-1])
    return float(laplacian.var())


def frontalness(keypoints):
    """1 for a frontal face, falling towards 0 as the nose moves outside the eyes."""
    points = np.asarray(keypoints, dtype=np.float64)
    eyes = points[1] - points[0]
    distance = np.hypot(*eyes)
    if distance <= 0:
        return 0.0
    offset = abs(np.dot(points[2] - (points[0] + points[1]) / 2, eyes / distance)) / distance
    return float(max(0.0, 1.0 - 2 * offset))


def eye_distance(keypoints):
    points = np.asarray(keypoints, dtype=np.float64)
    return float(np.hypot(*(points[1] - points[0])))


# GPU execution: as fast as the Neural Engine here (48 ms / 5 ms), and an ANE
# prediction was observed to hang indefinitely during the feasibility run.
DETECTOR_COREML = {'ModelFormat': 'MLProgram', 'MLComputeUnits': 'CPUAndGPU'}
RECOGNIZER_COREML = {'ModelFormat': 'MLProgram', 'MLComputeUnits': 'CPUAndGPU'}


class FaceEngine:
    """Sessions for fixed-size detectors and the recognizer.

    `directory` holds the verified published models; `derived` holds generated
    fixed-shape detectors and CoreML compilation caches (rebuildable).
    """
    def __init__(self, directory, derived, model, detector_sides, accelerate=True, verify=True):
        import onnxruntime
        directory, derived = Path(directory), Path(derived)
        derived.mkdir(parents=True, exist_ok=True)
        detector_name, detector_digest = model['detector']
        recognizer_name, recognizer_digest = model['recognizer']
        if verify:
            verify_file(directory / detector_name, detector_digest)
            verify_file(directory / recognizer_name, recognizer_digest)
        accelerate = accelerate and 'CoreMLExecutionProvider' in onnxruntime.get_available_providers()
        def session(path, coreml, cache):
            options = onnxruntime.SessionOptions()
            options.log_severity_level = 3
            providers = ['CPUExecutionProvider']
            if accelerate:
                (derived / cache).mkdir(exist_ok=True)
                providers.insert(0, ('CoreMLExecutionProvider', dict(coreml, ModelCacheDirectory=str(derived / cache))))
            return onnxruntime.InferenceSession(str(path), options, providers=providers)
        self.detectors = {}
        for side in detector_sides:
            static = derived / static_detector_name(side)
            if not static.exists():
                prepare_static_detector(directory / detector_name, static, side)
            self.detectors[side] = session(static, DETECTOR_COREML, f'coreml-detector-{side}')
        self.recognizer = session(directory / recognizer_name, RECOGNIZER_COREML, 'coreml-recognizer')
        self.accelerated = accelerate

    def detect(self, image, side):
        """Detect faces in an RGB PIL image letterboxed into the side x side detector."""
        from PIL import Image
        detector = self.detectors[side]
        width, height = image.size
        scale = letterbox_scale(width, height, side)
        resized = image if scale == 1 else image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
        canvas = np.zeros((side, side, 3), dtype=np.float32)
        canvas[:resized.height, :resized.width] = np.asarray(resized, dtype=np.float32)
        blob = ((canvas - DETECTOR_MEAN) / DETECTOR_STD).transpose(2, 0, 1)[None]
        outputs = detector.run(None, {detector.get_inputs()[0].name: blob})
        return decode(outputs, side, side, scale)

    def detect_scales(self, image, sides):
        return merge_scales([self.detect(image, side) for side in sides])

    def embed(self, crops):
        """L2-normalized embeddings and their raw norms for aligned uint8 crops."""
        if not len(crops):
            return np.zeros((0, EMBEDDING_DIMENSIONS), dtype=np.float32), np.zeros(0, dtype=np.float32)
        results = []
        name = self.recognizer.get_inputs()[0].name
        for start in range(0, len(crops), RECOGNIZER_BATCH):
            batch = np.stack(crops[start:start + RECOGNIZER_BATCH]).astype(np.float32)
            blob = ((batch - RECOGNIZER_MEAN) / RECOGNIZER_STD).transpose(0, 3, 1, 2)
            results.append(self.recognizer.run(None, {name: blob})[0])
        raw = np.concatenate(results).astype(np.float32)
        norms = np.linalg.norm(raw, axis=1)
        if raw.shape[1] != EMBEDDING_DIMENSIONS or not np.all(np.isfinite(raw)) or np.any(norms <= 0):
            raise ValueError('Recognizer returned invalid embeddings')
        return raw / norms[:, None], norms
