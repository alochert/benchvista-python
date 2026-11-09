# Filtered PWM and ADC sweep, a Spark Plug. CC0-1.0. Needs the Spark PWM/ADC task API.
# SAME 3.3 V board: OUT -> 10 kohm -> IN; IN -> 1 uF -> GND.
# Use a ceramic capacitor. Disconnect external circuits. Never connect 5 V.
# The RC filter averages PWM; without it ADC readings depend on pulse phase.
# 10..60% duty nominally means about 0.33..1.98 V on a 3.3 V supply.
# Prints raw scaled ADC counts, NOT calibrated volts or 16-bit accuracy.
def main(spark):
    settle = round(SETTINGS["settle"])
    samples = SETTINGS["samples"]
    print("duty_percent,adc_raw_mean")
    for percent in (10, 20, 30, 40, 50, 60, 50, 40, 30, 20, 10):
        spark.pwm(PINS["OUT"], 1000, round(percent * 65535 / 100))
        yield spark.sleep_ms(settle)
        total = 0
        for sample in range(samples):
            total += spark.adc_read_u16(PINS["IN"])
            yield spark.sleep_ms(5)
        print(percent, round(total / samples), sep=",")
    spark.pwm(PINS["OUT"], 1000, 0)
    yield spark.sleep_ms(100)
