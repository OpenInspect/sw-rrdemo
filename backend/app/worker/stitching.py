import base64
import math
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from scipy.sparse import lil_matrix
from scipy.sparse.linalg import lsqr

from app.schemas import StitchResult


class ScalablePCBStitcher:
    """Production-grade planar PCB stitcher adapted for in-memory Celery tasks."""

    def __init__(
        self,
        detector_name: str = "sift",
        feature_scale: float = 1.0,
        blend_mode: str = "feather",
        min_inliers: int = 8,
        inlier_ratio: float = 0.20,
    ):
        self.detector_name = detector_name.lower()
        self.feature_scale = feature_scale
        self.blend_mode = blend_mode
        self.min_inliers = min_inliers
        self.inlier_ratio = inlier_ratio

        if self.detector_name == "akaze":
            self.detector = cv2.AKAZE_create()
            self.norm_type = cv2.NORM_HAMMING
        elif self.detector_name == "orb":
            self.detector = cv2.ORB_create(nfeatures=3500)
            self.norm_type = cv2.NORM_HAMMING
        else:
            self.detector = cv2.SIFT_create(nfeatures=3500, contrastThreshold=0.03)
            self.norm_type = cv2.NORM_L2

    def extract_features(self, img: np.ndarray) -> Tuple[Tuple[int, int], np.ndarray, Optional[np.ndarray]]:
        h, w = img.shape[:2]

        if self.feature_scale < 0.99:
            proc_w = int(w * self.feature_scale)
            proc_h = int(h * self.feature_scale)
            proc_img = cv2.resize(img, (proc_w, proc_h), interpolation=cv2.INTER_AREA)
        else:
            proc_img = img

        gray = cv2.cvtColor(proc_img, cv2.COLOR_BGR2GRAY)
        kp, des = self.detector.detectAndCompute(gray, None)

        if des is None or len(kp) < 20:
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
            kp, des = self.detector.detectAndCompute(gray, None)

        inv_scale = 1.0 / self.feature_scale if self.feature_scale < 0.99 else 1.0
        coords = np.float32([k.pt for k in kp]) * inv_scale if kp else np.empty((0, 2), dtype=np.float32)

        return (h, w), coords, des

    def stitch_images(
        self,
        images: List[np.ndarray],
        grid_rows: Optional[int] = None,
        grid_cols: Optional[int] = None,
        scan_pattern: str = "serpentine",
    ) -> Optional[np.ndarray]:
        n_tiles = len(images)
        if n_tiles < 2:
            return None

        # Phase 1: Feature Extraction
        shapes = []
        all_coords = []
        all_descriptors = []

        for img in images:
            shape, coords, des = self.extract_features(img)
            shapes.append(shape)
            all_coords.append(coords)
            all_descriptors.append(des)

        tile_h, tile_w = shapes[0]

        # Phase 2: Pairwise Matching & Affine Estimation
        matcher = cv2.BFMatcher(self.norm_type)
        candidate_pairs = []

        if grid_rows and grid_cols and (grid_rows * grid_cols == n_tiles):
            grid_pairs = set()
            for r in range(grid_rows):
                for c in range(grid_cols):
                    idx = (r * grid_cols + (grid_cols - 1 - c)) if (scan_pattern == "serpentine" and r % 2 == 1) else (r * grid_cols + c)

                    if c + 1 < grid_cols:
                        n_idx = (r * grid_cols + (grid_cols - 1 - (c + 1))) if (scan_pattern == "serpentine" and r % 2 == 1) else (r * grid_cols + (c + 1))
                        grid_pairs.add(tuple(sorted((idx, n_idx))))

                    if r + 1 < grid_rows:
                        n_idx = ((r + 1) * grid_cols + (grid_cols - 1 - c)) if (scan_pattern == "serpentine" and (r + 1) % 2 == 1) else ((r + 1) * grid_cols + c)
                        grid_pairs.add(tuple(sorted((idx, n_idx))))
            candidate_pairs = [list(p) for p in grid_pairs]
        else:
            for i in range(n_tiles):
                for j in range(i + 1, n_tiles):
                    candidate_pairs.append([i, j])

        raw_edges = []
        for i, j in candidate_pairs:
            des_i, des_j = all_descriptors[i], all_descriptors[j]
            if des_i is None or des_j is None or len(des_i) < 5 or len(des_j) < 5:
                continue

            matches = matcher.knnMatch(des_i, des_j, k=2)
            good_matches = [m for m_n in matches if len(m_n) == 2 for m, n in [m_n] if m.distance < 0.75 * n.distance]

            if len(good_matches) < self.min_inliers:
                continue

            pts_i = all_coords[i][[m.queryIdx for m in good_matches]].reshape(-1, 1, 2)
            pts_j = all_coords[j][[m.trainIdx for m in good_matches]].reshape(-1, 1, 2)

            M, inliers = cv2.estimateAffinePartial2D(
                pts_j, pts_i,
                method=cv2.RANSAC,
                ransacReprojThreshold=3.0,
                maxIters=2000,
                confidence=0.99
            )

            if M is None or inliers is None:
                continue

            num_inliers = int(np.sum(inliers))
            ratio = num_inliers / len(good_matches)
            if num_inliers < self.min_inliers or ratio < self.inlier_ratio:
                continue

            scale = math.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2)
            rot_deg = math.degrees(math.atan2(M[1, 0], M[0, 0]))
            tx, ty = float(M[0, 2]), float(M[1, 2])

            if not (0.95 <= scale <= 1.05) or abs(rot_deg) > 4.5:
                continue

            overlap_x = max(0, min(tile_w, tx + tile_w) - max(0, tx))
            overlap_y = max(0, min(tile_h, ty + tile_h) - max(0, ty))
            if (overlap_x * overlap_y) < 0.04 * (tile_w * tile_h):
                continue

            raw_edges.append({
                "src": i, "dst": j,
                "dx": tx, "dy": ty,
                "inliers": num_inliers,
                "conf": float(num_inliers * ratio),
            })

        if len(raw_edges) < n_tiles - 1:
            return None

        # Phase 3: Maximum Spanning Tree (MST)
        sorted_edges = sorted(raw_edges, key=lambda e: e["conf"], reverse=True)
        parent = list(range(n_tiles))

        def find(x: int) -> int:
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        mst_adj: Dict[int, List[Tuple[int, float, float]]] = {k: [] for k in range(n_tiles)}
        mst_edges = []

        for e in sorted_edges:
            u, v = e["src"], e["dst"]
            root_u, root_v = find(u), find(v)
            if root_u != root_v:
                parent[root_u] = root_v
                mst_edges.append(e)
                mst_adj[u].append((v, e["dx"], e["dy"]))
                mst_adj[v].append((u, -e["dx"], -e["dy"]))

        if len(mst_edges) < n_tiles - 1:
            return None

        pos_x = np.zeros(n_tiles, dtype=np.float64)
        pos_y = np.zeros(n_tiles, dtype=np.float64)
        visited = {0}
        queue = [0]

        while queue:
            curr = queue.pop(0)
            for nbr, dx, dy in mst_adj[curr]:
                if nbr not in visited:
                    visited.add(nbr)
                    pos_x[nbr] = pos_x[curr] + dx
                    pos_y[nbr] = pos_y[curr] + dy
                    queue.append(nbr)

        verified_edges = [
            e for e in raw_edges
            if math.hypot(e["dx"] - (pos_x[e["dst"]] - pos_x[e["src"]]),
                          e["dy"] - (pos_y[e["dst"]] - pos_y[e["src"]])) <= 15.0
        ]

        # Phase 4: Least-Squares Bundle Adjustment
        num_edges = len(verified_edges)
        A = lil_matrix((num_edges + 1, n_tiles), dtype=np.float64)
        b_x = np.zeros(num_edges + 1, dtype=np.float64)
        b_y = np.zeros(num_edges + 1, dtype=np.float64)

        for row, e in enumerate(verified_edges):
            weight = math.sqrt(e["inliers"])
            A[row, e["dst"]] = 1.0 * weight
            A[row, e["src"]] = -1.0 * weight
            b_x[row] = e["dx"] * weight
            b_y[row] = e["dy"] * weight

        A[num_edges, 0] = 50.0
        b_x[num_edges] = 0.0
        b_y[num_edges] = 0.0

        A_csr = A.tocsr()
        sol_x = lsqr(A_csr, b_x)[0]
        sol_y = lsqr(A_csr, b_y)[0]

        margin = 25
        norm_x = sol_x - np.min(sol_x) + margin
        norm_y = sol_y - np.min(sol_y) + margin

        canvas_w = int(math.ceil(np.max(norm_x + tile_w) + margin))
        canvas_h = int(math.ceil(np.max(norm_y + tile_h) + margin))

        # Phase 5: Feathered Blending
        canvas_acc = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
        weight_acc = np.zeros((canvas_h, canvas_w), dtype=np.float32)

        mask = np.ones((tile_h, tile_w), dtype=np.uint8)
        mask[0, :] = 0; mask[-1, :] = 0; mask[:, 0] = 0; mask[:, -1] = 0
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        max_dist = np.max(dist)
        default_w_map = (dist / max_dist) ** 0.7 if (max_dist > 0 and self.blend_mode == "feather") else np.ones((tile_h, tile_w), dtype=np.float32)

        for i, img in enumerate(images):
            th, tw = img.shape[:2]
            tx = int(round(norm_x[i]))
            ty = int(round(norm_y[i]))

            w_map = default_w_map if (th, tw) == (tile_h, tile_w) else np.ones((th, tw), dtype=np.float32)

            c_x1 = max(0, tx)
            c_y1 = max(0, ty)
            c_x2 = min(canvas_w, tx + tw)
            c_y2 = min(canvas_h, ty + th)

            t_x1 = c_x1 - tx
            t_y1 = c_y1 - ty
            t_x2 = t_x1 + (c_x2 - c_x1)
            t_y2 = t_y1 + (c_y2 - c_y1)

            if c_x2 > c_x1 and c_y2 > c_y1:
                sub_img = img[t_y1:t_y2, t_x1:t_x2].astype(np.float32)
                sub_w = w_map[t_y1:t_y2, t_x1:t_x2]
                canvas_acc[c_y1:c_y2, c_x1:c_x2] += sub_img * sub_w[:, :, None]
                weight_acc[c_y1:c_y2, c_x1:c_x2] += sub_w

        valid = weight_acc > 1e-4
        final_img = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        final_img[valid] = np.clip(canvas_acc[valid] / weight_acc[valid, None], 0, 255).astype(np.uint8)
        return final_img


def stitch_b64(
    images_b64: list[str],
    rows: Optional[int] = None,
    cols: Optional[int] = None,
) -> StitchResult:
    """Decodes base64 tiles, stitches using planar global BA, and returns base64 JPEG."""
    images = []
    for b64 in images_b64:
        raw_bytes = base64.b64decode(b64)
        np_arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is not None:
            images.append(img)

    if len(images) < 2:
        return StitchResult(success=False, error="At least 2 valid images are required to stitch.")

    stitcher = ScalablePCBStitcher(detector_name="sift", blend_mode="feather")
    result_img = stitcher.stitch_images(images, grid_rows=rows, grid_cols=cols)

    if result_img is None:
        return StitchResult(success=False, error="Failed to align tiles into a single connected canvas.")

    success, buf = cv2.imencode(".jpg", result_img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not success:
        return StitchResult(success=False, error="Failed to encode composite image to JPEG.")

    result_b64 = base64.b64encode(buf).decode("utf-8")
    return StitchResult(success=True, image_b64=result_b64)