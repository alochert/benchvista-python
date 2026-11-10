# Seconds on an SH1106 OLED

Counts seconds on a 128 x 64 SH1106 OLED display, with a bar that fills, and prints each second.
It shows that a driver from the Library, the board's I2C bus and the display work.

The display's driver is not part of the board's Spark application. This Spark Plug names one
exact version of the Library's SH1106 driver, and BenchVista writes that driver into the file it
installs on the board. The board checks the driver before it loads it.

Wire the display at 3.3 V: SCL and SDA to the pads BenchVista shows when you run it, VCC to 3V3 and
GND to GND. Most display modules carry their own pull-up resistors. The display keeps its last
frame when the Spark Plug ends.

Needs a board with Spark application 0.3.0 or later.

| Setting          | Default | Range    |
| ---------------- | ------- | -------- |
| Seconds to count | 10      | 1 to 300 |
