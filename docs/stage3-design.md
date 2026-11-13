# Stage 3 — PCB features (design for approval)

Goal: KiCad / EasyEDA export → ready-to-cut jobs, no FlatCAM needed.
Auto-level (originally Stage 3) is already built in Stage 2.4.

User inputs (2026-11-13): KiCad and EasyEDA mostly; all tool types
(V-bits, end mills, drills); double-sided boards: yes; isolation and
copper clearing: both.

## 1. Import

| Item | Support |
|---|---|
| Gerber RS-274X | flashes, draws, arcs (G02/G03, G75), regions (G36/G37), aperture macros (KiCad / EasyEDA use: circle, rect, outline, polygon, thermal), polarity (LPD/LPC), step & repeat (SR) |
| Excellon | INCH/METRIC, LZ/TZ, tool table (T01C0.8), drills, routed slots (G85 / G00..M15 G01 M16) |
| Input | folder, single files, or the **.zip** both programs export |

### Layer detection by file name
| Layer | KiCad | EasyEDA |
|---|---|---|
| Top copper | `*-F_Cu.gbr`, `.gtl` | `Gerber_TopLayer.GTL` |
| Bottom copper | `*-B_Cu.gbr`, `.gbl` | `Gerber_BottomLayer.GBL` |
| Outline | `*-Edge_Cuts.gbr`, `.gm1` | `Gerber_BoardOutlineLayer.GKO` |
| Drills | `*-PTH.drl`, `*-NPTH.drl`, `*.drl` | `Drill_PTH_Through.DRL`, `Drill_NPTH_Through.DRL` |
| Ignored (shown greyed) | mask, paste, silk, fab, courtyard | same |

The user can change any layer assignment. All layers are put in one
coordinate system, with the board's lower-left corner at work 0,0
(optional offset).

## 2. Geometry engine
- Shapely 2 (GEOS) for polygons: union, buffer (offset), difference.
- Gerber → copper polygons (union of all features per layer).
- Arcs are approximated to 0.005 mm, the same as the G-code parser.

## 3. Isolation routing
- Parameters: tool, cut depth, passes (1–5), overlap %, extra gap, climb or
  conventional, cut inside the board outline only.
- **V-bit effective width** = tip + 2·depth·tan(angle/2); shown live,
  because the depth sets the width.
- Path = buffer(copper, w/2 + k·w·(1−overlap)) boundaries, ordered
  nearest-first to shorten rapids.
- **Not-cleared check**: copper gaps narrower than the tool are shown red,
  with the minimum gap size.
- Output: a `Program`, so preview, checks, simulate, auto-level and run all
  work as they do now.

## 4. Copper clearing (FlatCAM "NCC")
- Remove all copper not belonging to the nets: area = board − buffer(copper,
  gap). Filled with offset rings (contour-parallel, best for PCB), with
  optional line fill.
- Can use a bigger end mill for the bulk and the V-bit for the edges
  ("rest machining").
- Warning when the estimated time is long (the machine runs at about
  170 mm/min).

## 5. Drilling
- Holes grouped by size, ordered nearest-first per group.
- Per size, choose **drill** (plunge with a drill bit) or **mill**
  (circular pocket with an end mill, for holes larger than the tool).
  Small sizes can be merged into one drill to save tool changes.
- Peck drilling (step depth), drill depth = board thickness + 0.2 mm.
- A tool change between groups = pause (M6), then "Probe + set Z 0" and
  Resume. This needs the **jog-during-pause** feature (from 1.4), which
  is pulled into 3.3.

## 6. Board cutout
- Outline offset by the tool radius (outside), several depth passes,
  holding tabs (count / width / height), and the inside cut-outs of the
  board too.

## 7. Double-sided workflow
Wizard:
1. Choose the flip axis (X or Y) and the **alignment pin** positions
   (2 holes outside the board, on the flip axis).
2. Top side: drill the pin holes through the board **into the sacrifice
   plate**, then isolation (top), drilling.
3. Flip the board onto the pins.
4. Bottom side: the mirrored bottom copper, isolation, cutout.
- Mirroring is about the pin axis, so the board lands back exactly.
- Auto-level is measured again on side 2 (the height map is per side).

## 8. Tool library
- Tools: type (V-bit / end mill / drill), diameter, V angle and tip, feed,
  plunge feed, max step-down; seeded with common PCB tools.
- Presets per operation (isolation / clearing / drill / cutout), saved in
  `%APPDATA%\MicroPCB\tools.json`.

## 9. UI
- New **PCB** mode: a layer list with toggles and colours, an operations
  list (Isolation top, Drill, Cutout…) each with settings and a
  "Generate" button, and the results added to a job list.
- Jobs can also be saved as .nc files (to use elsewhere / to keep).

## 10. Milestones
| # | Deliverable | You test |
|---|---|---|
| 3.1 | Gerber + Excellon import, layer detection, layer viewer | open your KiCad & EasyEDA exports |
| 3.2 | Isolation routing + not-cleared check + tool library basics | compare with FlatCAM; cut a test board |
| 3.3 | Drilling + board cutout with tabs + jog during pause (tool change) | drill & cut out a board |
| 3.4 | Copper clearing (NCC, rest machining) | clear a board |
| 3.5 | Double-sided wizard with alignment pins | make a 2-layer board |

## 11. Test data needed
- One KiCad export and one EasyEDA export (Gerbers + drill files, as the
  .zip they make), ideally the same kind of board you normally make.
- Your tool list: V-bit angles / tips, end mill sizes, drill sizes.
- Board thickness you use (usually 1.6 mm) and the sacrifice plate.

## 12. Decisions (2026-11-14)
| # | Decision |
|---|---|
| 1 | Test data: KiCad project `samples/ECB_OLD_V1.0` (Gerber X2, mm, PTH + NPTH drills). `samples/` is git-ignored (private, 112 MB). EasyEDA sample later. |
| 2 | Drills: standard bits, 3.175 mm shank; sizes selectable/changeable per job |
| 3 | Double-sided: yes, selectable |
| 4 | Single-sided boards are made **bottom copper side up** → default job = bottom layer, mirrored |
| 5 | Depths, V-bit, passes etc.: always user-changeable (presets only as defaults) |
| 6 | Started 3.1 on 2026-11-14 |

Finding from the sample: the board outline is **190 × 126 mm**, bigger than
the 140 mm X travel → import offers **rotation** (0/90/180/270°) and
suggests 90° automatically when the board only fits rotated.

Layer roles come first from the Gerber X2 `TF.FileFunction` attribute
(KiCad, EasyEDA), then from the file name.

## 13. Milestone 3.1 — built (2026-11-14)
- `pcb/gerber.py`: RS-274X → shapely (apertures C/R/O/P, macros 1/4/5/7/20/21,
  regions, arcs, LPD/LPC, SR, X2 attributes incl. KiCad's `G04 #@!` form).
- `pcb/excellon.py`: tools, holes, G85 + routed slots, decimal/implied formats.
- `pcb/board.py`: folder / zip / files, roles (X2 first, file name second),
  outline from Edge.Cuts centre lines, placement (lower-left at work 0,0,
  rotation, mirror for bottom-up, offset). Mask / silk / paste are read
  only when shown (load 13 s → 1.8 s on the sample).
- GUI: **PCB** view tab (layers, holes, machine area) and **PCB** panel
  (open, layer table with role override, side, flip, rotation, offset,
  fit check, hole sizes).
- Sample board: 190 × 126 mm, 480 holes / 17 sizes, auto-rotated 90° →
  126 × 190 fits; bottom copper up (mirrored) by default.

## 14. Milestones 3.2–3.5 — built (2026-11-14)
| # | Built | Where |
|---|---|---|
| 3.2 | Isolation: V-bit width from depth, passes + overlap, extra gap, climb/conventional, clip to outline, nearest-first order. **Reach check** = copper islands closer than the tool width (STRtree), shown red in the PCB view. Tool library (V-bits, end mills, drills; editable, `tools.json`). | `pcb/cam.py`, `pcb/tools.py`, CAM tab |
| 3.3 | Drilling: holes → nearest available bit (tolerance), peck option, holes above a size milled (helix + ring). Cutout: outside offset, depth passes, tabs (count / width / height), inner cut-outs. **Tool change**: job pauses at M6 with the message; jog, spindle and *Probe + set Z 0* work while paused; Resume drives back to the path (Z up, X/Y, Z down) and the rest of the job follows the new Z zero. X/Y zero cannot change mid-job. | `cam.drilling/mill_holes/cutout`, `controller.resume_job`, `planner.shift` |
| 3.4 | Copper clearing: contour-parallel rings of (outline − copper − keep), optional smaller *rest* tool only where the big tool could not reach; time estimate. | `cam.clearing` |
| 3.5 | Double-sided: alignment pins on the flip axis (distance, diameter, depth into the plate), warnings when a pin is outside the machine area. **Side 1** (top up): pins, top isolation, drills. **Side 2** (flipped on the pins, mirrored): bottom isolation, clearing, cutout. | CAM tab |

### Workflow
**Single-sided (your usual, bottom copper up)**
1. PCB tab → Open → check *Side facing up = Bottom copper up*, rotation.
2. CAM tab → tick operations, choose tools/depths → **Generate job**.
3. Check the Checks / preview, auto-level if wanted, zero X/Y at the
   board corner and Z on the copper → Run. At each tool change: change the
   tool, Auto-level tab → *Probe + set Z 0*, Resume.

**Double-sided**
1. CAM tab → tick *Double-sided*, set pin size / distance; PCB tab offset
   so the pins are inside the machine area (for tall boards use the
   *Top ↔ bottom* flip direction).
2. **Side 1 job**: pins (drilled into the sacrifice plate), top isolation,
   drills.
3. Flip the board onto the pins — **do not change X/Y zero**.
4. **Side 2 job**: bottom isolation (mirrored), clearing, cutout.

Sample board (190 × 126 → 126 × 190): isolation of bottom copper ≈ 3 s,
full single-sided job (isolation + 9 drill sizes + milled holes + cutout)
generated in ≈ 8 s; 30° V-bit 0.1 mm passes the reach check, 60° 0.2 mm
reports 19 spots.
