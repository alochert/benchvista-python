# Console messages, a Spark Plug. CC0-1.0. No wiring needed.
def main(spark):
    for number in range(SETTINGS["messages"]):
        print("Spark", number + 1)
        yield spark.sleep_ms(round(SETTINGS["interval"]))
