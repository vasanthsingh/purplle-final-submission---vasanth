"""Preview 5 CCTV clips with YOLOv8-n bounding boxes."""
import cv2
import numpy as np
from pathlib import Path
from ultralytics import YOLO

# Camera mapping from the project setup
CLIP_MAP = {
    "CAM_ENTRY_01": "CAM 3.mp4",
    "CAM_FLOOR_SKIN": "CAM 1.mp4",
    "CAM_FLOOR_MAKEUP": "CAM 2.mp4",
    "CAM_CASH_COUNTER": "CAM 5.mp4",
    "CAM_STOCKROOM": "CAM 4.mp4"
}

def main():
    footage_dir = Path("data-provided/CCTV Footage")

    print("Loading YOLOv8 Nano model (optimised for preview speed)...")
    model = YOLO("yolov8n.pt")

    captures = {}
    print("Opening video files...")
    for cam, clip in CLIP_MAP.items():
        path = footage_dir / clip
        if path.exists():
            print(f" -> Found {clip}")
            captures[cam] = cv2.VideoCapture(str(path))
        else:
            print(f" -> Missing {path}")

    if not captures:
        print("No videos found! Ensure data-provided/CCTV Footage exists.")
        return

    tile_w, tile_h = 480, 270  # Resize each tile to fit on screen

    print("Starting preview... (Running 5 videos through AI will be slow!)")
    print("Press 'q' inside the video window to quit.")

    while True:
        tiles = []
        cam_list = list(captures.keys())

        for cam in cam_list:
            cap = captures[cam]
            # Skip frames to reduce lag
            for _ in range(5):
                ret, frame = cap.read()
                if not ret:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()

            # Detect people and draw bounding boxes
            results = model.predict(frame, classes=[0], verbose=False, conf=0.20, imgsz=320)
            if results:
                annotated = results[0].plot()
            else:
                annotated = frame

            resized = cv2.resize(annotated, (tile_w, tile_h))
            cv2.putText(resized, cam, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            tiles.append(resized)

        # Pad with black tiles for a 3x2 grid
        while len(tiles) < 6:
            tiles.append(np.zeros((tile_h, tile_w, 3), dtype=np.uint8))

        row1 = np.hstack((tiles[0], tiles[1], tiles[2]))
        row2 = np.hstack((tiles[3], tiles[4], tiles[5]))
        grid = np.vstack((row1, row2))

        window_title = "Vortex Analytics - YOLO Live Preview (Press 'q' to quit)"
        cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(window_title, cv2.WND_PROP_TOPMOST, 1)
        cv2.imshow(window_title, grid)

        print("Rendered frame... (check your taskbar if you don't see the window!)")

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    for cap in captures.values():
        cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
