# MicroCNC – safety features and checks

Everything MicroCNC checks or does to protect the bit, the board and the machine,
as of version 0.2.17. **Switch** = can be turned off in *Settings* (all are on
by default). Everything else is always on.

## 1. Before a job runs

| Check | What happens | Where |
|---|---|---|
| Position must be trusted | A job (and auto-level) will not start after a lost connection, released motors or an unexpected touch until work zero is set again (Zero all, or X, Y and Z). | Run, Auto-level |
| Machine must be idle | No job start while the machine is still moving. | Run |
| One job at a time | A second job cannot start while one is active. | Run |
| File has moves | Empty files are refused. | Run |
| Soft limits | With machine home set, a job that leaves the work area is refused. Without home set, the run question says *"Soft limits are OFF"*. **Switch:** Soft limits. | Run |
| Run confirmation | Shows the tool position (work X/Y/Z), estimated time, number of problems found, "spindle will switch on – keep hands clear". | Run |
| Probe clip reminder | After any probe touch: *"REMOVE THE PROBE CLIP"* (with the clip on, the machine stops going down at the copper). | Run |
| Job bigger than the height map | Warns which area is outside the map (edge height is used there). | Run, re-apply map |
| Old height map | Maps measured before 0.2.12 (Z counter read the wrong way round) warn on load: do not apply. | Load map |

## 2. G-code checks (Checks tab, red ✖ = problem, orange ⚠ = warning)

| Check | Level |
|---|---|
| Rapid (G0) sideways move below the surface – tool may break | ✖ |
| Rapid (G0) plunge into the material | ✖ |
| Cutting below the surface with the spindle OFF (no M3) | ✖ |
| Outside the machine work area | ✖ |
| Deeper than 2 mm (unusual for PCB work) | ⚠ |
| Feed above the machine maximum | ⚠ |
| With a height map: depths are checked below the *measured* copper, not below Z 0 | – |
| Camera obstacles (clamps marked in the Camera tab) under the toolpath | ⚠ |

## 3. Job options (per layer, in the Layers box; the file on disk is never changed)

| Option | What it does |
|---|---|
| Job origin: front-left corner = work 0 | Shifts the job so its first cut is at work 0 (travel moves are ignored). |
| Max cut depth | Cuts deeper than the limit are raised to it (V-bit: a deeper cut is a wider groove that eats thin tracks). On by default for isolation layers, **off by default for drill, alignment and cutout layers**. |
| Layer side | Each layer uses the height map of its own board side (Top / Bottom), so a bottom job never gets the top map. |
| Job summary | Above Start: layer, file, max cut depth, height map and part to run - what Start will do. |
| Height map (auto-level) | Added after the depth limit, so the depth stays the same below the copper everywhere. |
| Run part (⟦ Start here / End here ⟧ on the simulation bar) | Runs only those G-code lines: Z up to the safe height, X/Y to the start, spindle on again (with spin-up) if it is on at that point of the job, down (the last part into the copper slowly), and at the end Z up to the safe height and spindle off. The Run question says "Only PART of the job". Another file = whole job again. |

## 4. While a job runs

| Feature | What happens |
|---|---|
| Pause | Stops on the path (decelerates), position stays exact. |
| Resume in the copper | Z up to a safe height above where it stopped, X/Y back, Z down to the exact point, then the cut continues. |
| Resume above the copper | **Switch:** *Resume – paused above the copper*. Z goes up to the safe height (Settings), X/Y move straight to the start of the next cut, then Z comes down – it does not first go back down to where it was paused. |
| Spindle off during a pause | **Switch:** *Spindle switched off during a pause*. On resume the spindle is started at the safe height and given 2 s to spin up before the tool goes back down. |
| Jog during a pause | Allowed (only then). X/Y zero cannot be changed during a job (restored on resume); Z re-zero after a tool change moves the rest of the job with it. |
| Tool change (M6) | The job pauses with the message; Probe + set Z 0 works while paused. |
| Stop / E-STOP (Esc key) | Motion stops within about one packet, spindle is switched off. |
| Spindle overload | The machine's overload report stops motion and shows a warning. |
| Motors released (button) | The job fails; position is no longer trusted. |
| Connection lost | The job fails, position not trusted; automatic reconnect (**Switch:** Auto reconnect). |
| Keep-alive | Sent every second so the machine does not drop the connection. |
| Close the app while moving | Asks before closing. |
| Z drift check after a job | With the clip on: the tool goes back to the Z 0 point and touches it; warns if Z moved more than 0.03 mm. |

## 5. Jogging

| Feature | What happens |
|---|---|
| Low tool warning | **Switch:** *Low tool warning* (limit 0.5 mm). An X/Y jog (buttons or keyboard) with the tool below the limit asks first: *Cancel – I will lift Z* (default) or *Move anyway*. After *Move anyway* it is not asked again until Z has been above the limit. |
| Soft limits | Jogs are clamped to the work area (when home is set); the console says so. |
| Go to X / Y / Z | Z first goes up to the safe height, then X/Y move, Z goes to its value last; an empty box leaves that axis. A Z below work 0 asks first. Same soft limits as jogging. |
| Spindle switch | The lamp and the word show the state (Running / Stopped); the switch is only the action. |
| Jog blocked while a job runs | Only allowed when the job is paused. |
| Keyboard jog | Only while the key is held; stops on release. Not active while typing in a field. |

## 6. Probing and auto-level

| Feature | What happens |
|---|---|
| Spindle must be off | Probing refuses to start with the spindle on. |
| Touch left over from before | After connecting, the first probe clears a touch the machine may still hold (it otherwise ignores Z-down and reports no touch). |
| Clip still touching | If the probe still reports contact before starting: *"Lift Z and try again"*. |
| No touch | Stops after the set travel: *"is the clip on the copper and the tool above the board?"*. |
| Counter jump | A probe counter jump of more than 5 mm (machine power-cycled) stops and asks to re-zero. |
| Points outside the work area | Auto-level refuses a point that soft limits would cut off. |
| Lift before every move | Z lifts by the clearance before each sideways move between points. |
| Outlier re-probe | A point more than 0.06 mm off its neighbours is probed again; still off = reported as suspect (dirt, burr). |
| Closing check | The first point is probed again at the end; more than 0.03 mm off = map NOT applied (Z lost steps). |
| Use job size | Uses only the cut area (no probe points outside the board from start / travel moves). |
| Job-based points | Fewer points where nothing is fine; points added automatically where the board bends. |

## 7. CAM (MicroCNC CAM)

| Check | What happens |
|---|---|
| Too narrow for the tool | Places where the isolation tool cannot pass between copper are listed in Checks (click to zoom) and drawn red. |
| Board check | Smallest clearance, track, pad, drill and annular ring per side, compared with the isolation tool. |
| Suggest tool + depth | Picks the tool and depth that separate every track. |
| Variable depth by gap (optional) | Shallower only in narrow gaps; tracks are never thinned. |
| Side file check | A side file that does not match the G-code (edited since) is not used. |

## Settings – safety switches

*Settings* dialog, all on by default:

- Soft limits
- Auto reconnect
- Resume: paused above the copper – resume at safe height to the next cut
- Resume: spindle switched off during a pause – start it again before going down
- Low tool warning (and its limit, default 0.5 mm)
- Safe height (also used for resume and Go to work zero)
- Height maps: save every finished auto-level map automatically (Documents\MicroPCB height maps; a map whose closing check failed is saved with -DRIFT in its name)
