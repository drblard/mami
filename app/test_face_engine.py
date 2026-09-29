import math
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    import face_engine
except ImportError:
    np = None


@unittest.skipIf(np is None, 'NumPy/Pillow face checks run where the worker runtime is installed')
class FaceEngineTests(unittest.TestCase):
    def test_static_detector_fixes_input_and_reinfers_declared_outputs(self):
        try:
            import onnx
            from onnx import helper, TensorProto
        except ImportError:
            self.skipTest('onnx is only needed when preparing detector models')
        source_input = helper.make_tensor_value_info('input.1', TensorProto.FLOAT, [1, 3, 'height', 'width'])
        wrong_output = helper.make_tensor_value_info('score', TensorProto.FLOAT, [1, 3, 640, 640])
        graph = helper.make_graph([helper.make_node('Relu', ['input.1'], ['score'])], 'detector', [source_input], [wrong_output])
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source.onnx', Path(directory) / 'fixed.onnx'
            onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid('', 13)]), str(source))
            face_engine.prepare_static_detector(source, target, 1920)
            fixed = onnx.load(str(target))
            shape = lambda value: [d.dim_value for d in value.type.tensor_type.shape.dim]
            self.assertEqual(shape(fixed.graph.input[0]), [1, 3, 1920, 1920])
            self.assertEqual(shape(fixed.graph.output[0]), [1, 3, 1920, 1920])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['fixed.onnx', 'source.onnx'])

    def test_letterbox_scale_fits_without_upscaling(self):
        self.assertEqual(face_engine.letterbox_scale(4000, 3000, 1920), 0.48)
        self.assertEqual(face_engine.letterbox_scale(1080, 1920, 1920), 1.0)
        self.assertEqual(face_engine.letterbox_scale(100, 50, 640), 1.0)
        for arguments in ((0, 10, 640), (10, 10, 650)):
            with self.assertRaises(ValueError):
                face_engine.letterbox_scale(*arguments)

    def outputs(self, width, height, positives):
        """Synthetic SCRFD outputs; positives maps (stride, anchor) to (score, box distances, keypoint distances)."""
        scores, boxes, points = [], [], []
        for stride in face_engine.DETECTOR_STRIDES:
            count = (width // stride) * (height // stride) * face_engine.DETECTOR_ANCHORS_PER_CELL
            score = np.zeros((count, 1), np.float32); box = np.zeros((count, 4), np.float32); point = np.zeros((count, 10), np.float32)
            for (positive_stride, anchor), (value, distances, keypoints) in positives.items():
                if positive_stride == stride:
                    score[anchor] = value; box[anchor] = distances; point[anchor] = keypoints
            scores.append(score); boxes.append(box); points.append(point)
        return scores + boxes + points

    def test_decode_places_boxes_and_keypoints_in_source_pixels(self):
        # Stride 8, 64x64 input: anchor 18 is the first anchor of cell (row 1, column 1) -> center (8, 8).
        outputs = self.outputs(64, 64, {(8, 18): (0.9, [1, 1, 2, 3], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]),
                                        (16, 0): (0.4, [1, 1, 1, 1], [0] * 10)})
        faces = face_engine.decode(outputs, 64, 64, 0.5)
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0].box, (0.0, 0.0, 48.0, 64.0))
        self.assertEqual(faces[0].keypoints, ((16.0, 16.0), (32.0, 32.0), (48.0, 48.0), (64.0, 64.0), (80.0, 80.0)))
        self.assertAlmostEqual(faces[0].score, 0.9, places=6)

    def test_overlapping_detections_keep_the_highest_score(self):
        same = ([1, 1, 1, 1], [0] * 10)
        outputs = self.outputs(64, 64, {(8, 18): (0.7, *same), (8, 19): (0.8, *same), (8, 0): (0.6, *same)})
        faces = face_engine.decode(outputs, 64, 64, 1.0)
        self.assertEqual([round(face.score, 6) for face in faces], [0.8, 0.6])

    def test_scale_merge_keeps_complementary_faces_and_best_overlap(self):
        Face = face_engine.Face
        points = ((0, 0),) * 5
        large_low = Face((100, 100, 800, 900), points, 0.9)
        small_high = Face((1500, 200, 1540, 250), points, 0.7)
        large_high = Face((110, 105, 790, 905), points, 0.6)
        merged = face_engine.merge_scales([[large_low], [small_high, large_high]])
        self.assertEqual(merged, [large_low, small_high])
        self.assertEqual(face_engine.merge_scales([[], []]), [])

    def test_detector_output_count_is_checked(self):
        with self.assertRaises(ValueError):
            face_engine.decode([np.zeros((1, 1))] * 8, 64, 64, 1.0)

    def test_similarity_transform_recovers_rotation_scale_and_translation(self):
        angle = math.radians(30)
        expected = np.array([[2 * math.cos(angle), -2 * math.sin(angle), 5], [2 * math.sin(angle), 2 * math.cos(angle), -7]])
        source = face_engine.ARCFACE_TEMPLATE
        target = source @ expected[:, :2].T + expected[:, 2]
        np.testing.assert_allclose(face_engine.similarity_transform(source, target), expected, atol=1e-9)

    def test_mirrored_keypoints_do_not_produce_a_reflection(self):
        mirrored = face_engine.ARCFACE_TEMPLATE * [-1, 1]
        matrix = face_engine.similarity_transform(mirrored, face_engine.ARCFACE_TEMPLATE)
        self.assertGreater(np.linalg.det(matrix[:, :2]), 0)

    def test_alignment_maps_scaled_shifted_landmarks_onto_the_template(self):
        pixels = np.zeros((400, 400, 3), np.uint8)
        keypoints = face_engine.ARCFACE_TEMPLATE * 2 + [50, 60]
        for x, y in np.rint(keypoints).astype(int):
            pixels[y - 2:y + 3, x - 2:x + 3] = 255
        aligned = face_engine.align(Image.fromarray(pixels), keypoints)
        for x, y in np.rint(face_engine.ARCFACE_TEMPLATE).astype(int):
            self.assertEqual(aligned[y, x].tolist(), [255, 255, 255])
            self.assertEqual(aligned[y + 4, x].tolist(), [0, 0, 0])

    def test_sharpness_distinguishes_flat_and_detailed_crops(self):
        self.assertEqual(face_engine.sharpness(np.full((112, 112, 3), 90, np.uint8)), 0.0)
        board = ((np.indices((112, 112)).sum(axis=0) % 2) * 255).astype(np.uint8)
        self.assertAlmostEqual(face_engine.sharpness(np.repeat(board[..., None], 3, axis=2)), 1020.0 ** 2, places=3)

    def test_frontalness_and_eye_distance(self):
        template = face_engine.ARCFACE_TEMPLATE
        self.assertAlmostEqual(face_engine.eye_distance(template), math.hypot(35.2372, -0.1949), places=9)
        self.assertGreater(face_engine.frontalness(template), 0.99)
        profile = template.copy(); profile[2] = template[1]
        self.assertAlmostEqual(face_engine.frontalness(profile), 0.0, places=12)


if __name__ == '__main__':
    unittest.main()
