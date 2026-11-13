(MicroPCB air-cut test - milestone 1.3)
(Set work zero with the tool ~10 mm ABOVE the bed, nothing clamped.)
(Cuts happen at Z -0.2 relative to zero = still in the air.)
(Size 30 x 20 mm, starts at work zero. Takes about 2 minutes.)
G21 G90 G17
G0 Z2
G0 X0 Y0
M3
G4 P1
(rectangle)
G1 Z-0.2 F60
G1 X30 F150
G1 Y20
G1 X0
G1 Y0
G0 Z2
(circle r=5 centred at 15,10)
G0 X10 Y10
G1 Z-0.2 F60
G2 X10 Y10 I5 J0 F120
G0 Z2
(diagonal)
G0 X0 Y0
G1 Z-0.2 F60
G1 X30 Y20 F150
G0 Z5
M0 (pause test - press Resume)
G0 X0 Y0
M5
M30
