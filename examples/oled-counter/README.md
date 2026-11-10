# OLED counter

Counts from 1 to the number of counts on a 128 x 64 SSD1306 OLED display, and prints each count.
It shows that a board's display driver, its I2C bus and the display work.

Wire the display at 3.3 V: SCL and SDA to the pads BenchVista shows when you run it, VCC to 3V3 and
GND to GND. Most display modules carry their own pull-up resistors. The display keeps its last
frame when the Spark Plug ends.

Needs a board with Spark application 0.2.0 or later.

| Setting             | Default | Range           |
| ------------------- | ------- | --------------- |
| Counts              | 10      | 1 to 100        |
| Time between counts | 500 ms  | 100 to 5,000 ms |
