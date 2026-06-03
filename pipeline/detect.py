"""Per-clip detection → track → region → event dispatch.

Model: YOLOv8-m (Ultralytics build). Filters to class==person.
Subsamples to 5 fps to keep CPU runtime tractable — events are
aligned to original wall-clock via `--start-ts` so downstream
analytics see realistic timestamps.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .cross_camera import OverlapFilter
from .emit import EventDispatcher, DispatcherSettings, create_activity
from .reentry import ReturnVisitorBuffer
from .staff import EmployeeDetector, WorkwearColor
from .tracker import MotionTracker, map_track_to_person
from .zones import BoundaryDetector, RegionTracker, bounding_box_midpoint, is_point_inside_region


def _load_layout(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _hsv_signature(hsv_patch) -> tuple[float, ...]:
    """3-bin H-channel histogram — cheap but sufficient for short-window re-ID."""
    try:
        import numpy as np  # type: ignore
        h = hsv_patch[..., 0].ravel()
        bins, _ = np.histogram(h, bins=3, range=(0, 180))
        total = bins.sum() or 1
        return tuple(float(b / total) for b in bins)
    except Exception:
        return (0.0, 0.0, 0.0)


def _avg_hsv(hsv_patch) -> tuple[float, float, float]:
    try:
        import numpy as np  # type: ignore
        return (
            float(np.mean(hsv_patch[..., 0])),
            float(np.mean(hsv_patch[..., 1])),
            float(np.mean(hsv_patch[..., 2])),
        )
    except Exception:
        return (0.0, 0.0, 0.0)


def analyze_footage(
    *,
    clip_path: Path,
    camera_id: str,
    store_id: str,
    layout: dict[str, Any],
    dispatcher: EventDispatcher,
    return_buffer: ReturnVisitorBuffer,
    overlap_filter: OverlapFilter | None = None,
    start_ts: datetime,
    fps_sample: float = 5.0,
    detector_factory=None,
) -> dict[str, int]:
    """Run inference on a single clip. Returns dispatch stats.

    `detector_factory` allows tests to inject a mock yielding (frame_idx, detections, hsv_patch_fn).
    """
    import cv2  # type: ignore

    cam_cfg = layout["cameras"][camera_id]
    zones_cfg = cam_cfg.get("zones", [])
    force_staff = bool(cam_cfg.get("force_is_staff", False))
    entry_line_cfg = cam_cfg.get("entry_line")
    entry_boundary: Optional[BoundaryDetector] = None
    if entry_line_cfg:
        a, b = entry_line_cfg["points"]
        n = entry_line_cfg.get("inside_normal", [0, 1])
        entry_boundary = BoundaryDetector(a=tuple(a), b=tuple(b), inside_normal=tuple(n))

    region_tracker = RegionTracker()
    staff_cfg = layout.get("staff_uniform_hsv") or {}
    workwear = WorkwearColor(
        h_range=tuple(staff_cfg.get("h_range", [0, 179])),
        s_range=tuple(staff_cfg.get("s_range", [0, 255])),
        v_range=tuple(staff_cfg.get("v_range", [0, 255])),
    ) if staff_cfg else None
    employee_det = EmployeeDetector(uniform=workwear, force_is_staff=force_staff)

    motion_tracker = MotionTracker(min_confidence=0.25)

    # Detector: YOLOv8 Medium via Ultralytics unless a factory is injected.
    if detector_factory is None:
        detector_factory = _default_yolo_factory

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {clip_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(int(round(src_fps / fps_sample)), 1)

    detect_fn = detector_factory()

    frame_idx = 0
    current_queue_depth = 0
    in_billing_since: dict[str, int] = {}
    dispatched = 0
    start_wall = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step != 0:
            frame_idx += 1
            continue

        t_ms = int(1000 * frame_idx / src_fps)
        wall_ts = start_ts + timedelta(milliseconds=t_ms)

        hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # detection
        detections = detect_fn(frame)
        tracks = motion_tracker.update(detections)

        # per-zone + entry logic
        queue_occupants = set()
        for tk in tracks:
            visitor_id = map_track_to_person(tk.track_id)
            cx, cy = bounding_box_midpoint(tk.bbox)
            x1, y1, x2, y2 = (int(max(0, v)) for v in tk.bbox)
            patch = hsv_frame[y1:y2, x1:x2] if y2 > y1 and x2 > x1 else hsv_frame[0:1, 0:1]
            avg_hsv = _avg_hsv(patch)
            is_staff_flag = employee_det.classify(visitor_id, avg_hsv)

            # Entry / exit via boundary line
            if entry_boundary is not None:
                evt = entry_boundary.update(visitor_id, (cx, cy))
                if evt == "enter":
                    sig = _hsv_signature(patch)
                    prior = return_buffer.lookup(sig, t_ms)
                    if prior is not None and prior != visitor_id:
                        dispatcher.emit(create_activity(
                            store_id=store_id, camera_id=camera_id, visitor_id=prior,
                            event_type="REENTRY", timestamp=wall_ts, is_staff=is_staff_flag,
                            confidence=tk.confidence,
                            metadata={"prior_visitor_id": prior, "new_track_id": visitor_id},
                        ))
                    else:
                        dispatcher.emit(create_activity(
                            store_id=store_id, camera_id=camera_id, visitor_id=visitor_id,
                            event_type="ENTRY", timestamp=wall_ts, is_staff=is_staff_flag,
                            confidence=tk.confidence,
                        ))
                    dispatched += 1
                elif evt == "exit":
                    sig = _hsv_signature(patch)
                    return_buffer.record_exit(visitor_id, sig, t_ms)
                    dispatcher.emit(create_activity(
                        store_id=store_id, camera_id=camera_id, visitor_id=visitor_id,
                        event_type="EXIT", timestamp=wall_ts, is_staff=is_staff_flag,
                        confidence=tk.confidence,
                    ))
                    dispatched += 1

            # Region checks (with overlap suppression)
            for z in zones_cfg:
                polygon = [tuple(p) for p in z["polygon"]]
                inside = is_point_inside_region((cx, cy), polygon)
                if inside:
                    employee_det.record_zone(visitor_id, z["id"], t_ms)
                for etype, dwell in region_tracker.on_zone_event(visitor_id, z["id"], inside, t_ms):
                    # Suppress if same visitor+zone dispatched recently by another camera
                    if overlap_filter and etype == "ZONE_ENTER":
                        if not overlap_filter.should_emit(visitor_id, z["id"], t_ms):
                            continue
                    dispatcher.emit(create_activity(
                        store_id=store_id, camera_id=camera_id, visitor_id=visitor_id,
                        event_type=etype, timestamp=wall_ts, zone_id=z["id"],
                        dwell_ms=dwell, is_staff=is_staff_flag, confidence=tk.confidence,
                        metadata={"sku_zone": z.get("sku_category")} if z.get("sku_category") else {},
                    ))
                    dispatched += 1

                # Billing queue logic
                if z.get("type") == "billing":
                    if inside:
                        queue_occupants.add(visitor_id)

        # Queue depth + JOIN / ABANDON tracking
        if any(z.get("type") == "billing" for z in zones_cfg):
            prev_in_billing = set(in_billing_since.keys())
            now_in_billing = queue_occupants
            joined = now_in_billing - prev_in_billing
            left = prev_in_billing - now_in_billing
            current_queue_depth = len(now_in_billing)
            for vid in joined:
                in_billing_since[vid] = t_ms
                dispatcher.emit(create_activity(
                    store_id=store_id, camera_id=camera_id, visitor_id=vid,
                    event_type="BILLING_QUEUE_JOIN", timestamp=wall_ts,
                    zone_id="ZONE_BILLING",
                    is_staff=employee_det.classify(vid), confidence=0.9,
                    metadata={"queue_depth": current_queue_depth},
                ))
                dispatched += 1
            for vid in left:
                started = in_billing_since.pop(vid, t_ms)
                dwell = t_ms - started
                # Very short queue visits don't count as "abandon".
                event_type = "BILLING_QUEUE_LEAVE" if dwell < 5_000 else "BILLING_QUEUE_ABANDON"
                dispatcher.emit(create_activity(
                    store_id=store_id, camera_id=camera_id, visitor_id=vid,
                    event_type=event_type, timestamp=wall_ts,
                    zone_id="ZONE_BILLING", dwell_ms=dwell,
                    is_staff=employee_det.classify(vid), confidence=0.9,
                    metadata={"queue_depth_at_leave": current_queue_depth},
                ))
                dispatched += 1

        # Periodic prune of overlap filter cache
        if overlap_filter and frame_idx % 50 == 0:
            overlap_filter.prune(t_ms)

        if frame_idx % 100 == 0:
            print(f"[analyze] {camera_id}: processed {frame_idx} frames...", flush=True)

        frame_idx += 1

    cap.release()
    dispatcher.flush()
    return {"frames": frame_idx, "emitted": dispatched, "elapsed_sec": int(time.time() - start_wall)}


def _default_yolo_factory():
    """Lazy-load YOLOv8 Medium from Ultralytics for faster inference."""
    from ultralytics import YOLO  # type: ignore

    model = YOLO("yolov8m.pt")

    def detect(frame) -> list[tuple[tuple[float, float, float, float], float]]:
        # Use imgsz=320 to massively speed up processing on CPU
        results = model.predict(frame, classes=[0], verbose=False, conf=0.20, imgsz=320)
        out: list[tuple[tuple[float, float, float, float], float]] = []
        if not results:
            return out
        r = results[0]
        if r.boxes is None:
            return out
        for i in range(len(r.boxes)):
            x1, y1, x2, y2 = (float(v) for v in r.boxes.xyxy[i].tolist())
            conf = float(r.boxes.conf[i].item())
            out.append(((x1, y1, x2, y2), conf))
        return out

    return detect


# ------------------------------- CLI ----------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Per-clip detection pipeline")
    p.add_argument("--clip", required=True, type=Path)
    p.add_argument("--camera-id", required=True)
    p.add_argument("--store-id", default="STORE_001")
    p.add_argument("--layout", type=Path, default=Path("config/store_layout.json"))
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--jsonl", type=Path, default=Path("data/events.jsonl"))
    p.add_argument("--start-ts", default=None, help="ISO8601 start; default=now-UTC")
    p.add_argument("--fps", type=float, default=5.0)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    layout = _load_layout(args.layout)
    start_ts = datetime.fromisoformat(args.start_ts) if args.start_ts else datetime.now(timezone.utc)
    return_buffer = ReturnVisitorBuffer()
    overlap_filter = OverlapFilter()
    cfg = DispatcherSettings(api_url=args.api_url, jsonl_path=args.jsonl)
    with EventDispatcher(cfg) as dispatcher:
        stats = analyze_footage(
            clip_path=args.clip,
            camera_id=args.camera_id,
            store_id=args.store_id,
            layout=layout,
            dispatcher=dispatcher,
            return_buffer=return_buffer,
            overlap_filter=overlap_filter,
            start_ts=start_ts,
            fps_sample=args.fps,
        )
    print(json.dumps({"camera_id": args.camera_id, **stats, **dispatcher.stats}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
