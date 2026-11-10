# OLED counter, a Spark Plug. CC0-1.0.
# A 128 x 64 SSD1306 OLED on I2C, at 3.3 V: SCL and SDA to the pads BenchVista shows,
# VCC to 3V3 and GND to GND. Most modules carry their own pull-up resistors.
def main(spark):
    display = spark.device("DISPLAY")
    for number in range(SETTINGS["count"]):
        display.fill(0)
        display.text("BenchVista", 0, 0, 1)
        display.text("Count " + str(number + 1), 0, 16, 1)
        display.show()
        print("count", number + 1)
        yield spark.sleep_ms(round(SETTINGS["interval"]))
