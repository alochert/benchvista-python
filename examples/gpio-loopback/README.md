# Digital loopback test

Connect OUT → 1 kΩ resistor → IN, both on the same 3.3 V board. Disconnect external circuits
and never connect 5 V to either pin.

The Spark Plug writes low and high, waits 5, 10, 25, 50 and 100 ms, and prints twenty
comparisons followed by `PASS` or `FAIL` and the number of mismatches. It ends with OUT low;
then the board releases both pins. This checks logic transfer, not timing accuracy.
