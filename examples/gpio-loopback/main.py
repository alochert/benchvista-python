# Digital loopback test, a Spark Plug. CC0-1.0.
# OUT -> 1 kohm resistor -> IN on the SAME 3.3 V board.
# Disconnect external circuits. This is a logic check, not a timing measurement.
def main(spark):
    failures = 0
    for delay in (5, 10, 25, 50, 100):
        for level in (0, 1, 0, 1):
            spark.write(PINS["OUT"], level)
            yield spark.sleep_ms(delay)
            observed = spark.read(PINS["IN"])
            if observed != level:
                failures += 1
            print("delay_ms", delay, "sent", level, "read", observed)
    spark.write(PINS["OUT"], 0)
    print("PASS" if failures == 0 else "FAIL", "mismatches", failures)
