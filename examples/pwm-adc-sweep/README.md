# Filtered PWM and ADC sweep

Connect OUT → 10 kΩ resistor → IN, with a 1 µF ceramic capacitor from IN to GND, all on the
same 3.3 V board. Disconnect external circuits and never connect 5 V to either pin.

The Spark Plug steps PWM duty from 10 % to 60 % and back at 1 kHz. After each step it waits the
settling time, averages the ADC readings and prints `duty_percent,adc_raw_mean`. The RC filter
averages the PWM; without it the readings depend on the pulse phase. The values are raw scaled
ADC counts, not calibrated volts and not 16-bit accuracy.

| Setting                | Default | Range         |
| ---------------------- | ------- | ------------- |
| Settling time per step | 250 ms  | 50 to 1000 ms |
| ADC readings per step  | 8       | 1 to 32       |
