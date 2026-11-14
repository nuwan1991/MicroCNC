# Stage 4 – USB camera: alignment and obstacle avoidance (design draft)

Status: **4.1–4.5 built** (simulated camera tested; not yet tested with a real camera).

## 4.1 Camera basics
- Any UVC USB camera (OpenCV `cv2.VideoCapture`, new dependency `opencv-python`).
- Camera tab next to 2D / 3D / PCB: live picture, crosshair, zoom, exposure/focus if the camera supports it.
- Choose camera, resolution, rotation/mirror of the picture.

## 4.2 Calibration (done once per camera mount)
- **Lens distortion**: optional, print a checkerboard, take ~10 pictures.
- **Scale (mm per pixel)**: jog the machine a known distance over a mark; the app measures how far the mark moved in the picture. Gives scale and camera rotation versus machine axes.
- **Camera → spindle offset** (camera on the gantry): mark a point by drilling/scratching with the tool, jog the camera over it, click it. Offset stored in settings.
- Fixed overhead camera alternative: 4-point homography from marks at known machine positions.

## 4.3 Alignment (fiducials)
- Click (or auto-detect circle) two or three reference points on the real board: drilled holes, pads or pins.
- App matches them with the same points in the loaded PCB and computes rotation + offset (+ optional scale check); the job is transformed before running, like the height map.
- Main use: **side 2 of double-sided boards** and re-fixing a board that moved, without alignment pins.
- "Jog to point" button: drives camera over each expected point, user confirms/corrects.
- Shows residual error in mm; warns above a limit.

## 4.4 Obstacle avoidance
- Capture a picture of the whole bed (overhead camera, or stitched from gantry camera tiles).
- Detect clamps / screws / tape: user paints or the app suggests areas (colour/edge difference against an empty-bed reference picture).
- Obstacles stored as polygons with a height. Then:
  - **Checks tab** warns if a cut or the board outline hits an obstacle.
  - **Rapid moves** are rerouted: raise Z above the obstacle height, or path around it.
  - Board placement can be moved to a free area.

## 4.5 Overlay
- Toolpath and PCB drawn on top of the live camera picture, so you can see before running where it will cut.

## Decisions (user answers)
1. **Camera fixed above the bed.** Calibration = homography (pixel → machine X/Y) from ≥4 points: the
   tool tip is jogged to positions, user clicks the tip in the picture (or the app finds marks).
   No camera→spindle offset needed. Board copper height (Z) parallax corrected with board thickness.
2. **Fixed / manual focus first**; autofocus controls later, only if the camera supports them.
3. **Alignment points: drilled holes and copper pads** (auto circle / pad detection + manual click).
4. **Obstacles: warning only first**; reroute / skip later.
5. **One safe height** for all obstacles first.
6. **Multi-camera later** – code keeps a camera list + per-camera calibration from the start.

## Build order
- 4.1 Camera tab (live view, camera select, resolution, focus/exposure if available).
- 4.2 Bed calibration (homography, lens distortion optional) – saved per camera.
- 4.3 Alignment from holes/pads → job rotation + offset, residual error shown.
- 4.4 Obstacle marking on bed picture → Checks tab warnings, rapids at safe height.
- 4.5 PCB / toolpath overlay on the live picture.

## Implementation notes (4.1–4.5)
- Code: `camera/device.py` (cameras, simulated camera), `camera/calib.py`, `camera/align.py`,
  `camera/obstacles.py`; UI `ui/camera_view.py` (top tab "Camera"), `ui/camera_panel.py`
  (bottom tab "Camera"). Settings in `%APPDATA%/MicroPCB/camera.json`, one calibration per camera.
- **Moving bed:** tick the axes that move the table. Frame mm = bed mm at the reference position
  (machine position at the first calibration click); bed = frame + (pos − ref) on those axes.
  With a moving bed use the **mark job** (spots cut 0.1 mm deep at 9 known places); the
  tool-tip method only works for axes that move the spindle.
- Calibration uses machine coordinates: keep machine home (Set home) the same afterwards.
- **Parallax:** camera height + board thickness; the board top looks larger than the bed.
- **Alignment:** click a hole/pad → blob centre is found (Otsu, several window sizes) →
  matched to the nearest PCB hole/pad within 4 mm → least-squares rotation + offset; residual
  per point, scale reported as a calibration check. "Apply to job" rotates/shifts X/Y before
  the height map. Accuracy is limited by the picture: ~0.26 mm/pixel at 1280×720 over the
  whole bed; expect ~0.1 mm.
- **Obstacles:** empty-bed picture → Detect (picture difference; the board outline is ignored),
  or draw rectangles. Checks tab warns for moves below the safe height over an obstacle and
  for a board lying over one. Rerouting comes later.
- Simulated camera: renders the bed, marks, the loaded board (placed 0.8°, +1.2/−0.6 mm wrong
  on purpose) and two clamps after the empty-bed picture.
