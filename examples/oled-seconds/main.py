# Seconds on an SH1106 OLED, a Spark Plug. CC0-1.0.
# A 128 x 64 SH1106 OLED on I2C, at 3.3 V: SCL and SDA to the pads BenchVista shows,
# VCC to 3V3 and GND to GND. BenchVista installs the SH1106 driver with this file.
def main(spark):
    display = spark.device("DISPLAY")
    for second in range(SETTINGS["seconds"] + 1):
        display.fill(0)
        display.text("BenchVista", 0, 0, 1)
        display.text(str(second) + " s", 0, 24, 1)
        display.rect(0, 48, display.width, 12, 1)
        display.fill_rect(2, 50, (display.width - 4) * second // SETTINGS["seconds"], 8, 1)
        display.show()
        print("second", second)
        yield spark.sleep_ms(1000)
