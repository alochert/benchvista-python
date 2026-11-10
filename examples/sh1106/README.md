# SH1106 OLED display

A Spark driver for 128 x 64 SH1106 OLED displays on I2C. These are the usual 1.3 inch modules. An
SSD1306 driver shows their picture shifted by two columns, because the SH1106 holds 132 columns
and its panel shows the middle 128.

A Spark Plug names the driver by its exact version in `devices` and draws with the same calls as
on any display: `fill`, `pixel`, `hline`, `vline`, `line`, `rect`, `fill_rect`, `text` and `show`.

```json
{
  "role": "DISPLAY",
  "driver": { "id": "sh1106", "version": "1.0.0", "sha256": "<the hash benchvista check prints>" },
  "label": "128 x 64 SH1106 OLED display"
}
```

Wire the display at 3.3 V: SCL and SDA to the pads BenchVista shows, VCC to 3V3 and GND to GND.
The address is 0x3C, or 0x3D on a module strapped for it.

`show()` sends the frame in eight pages, 1,064 bytes in all, at 400 kHz.

Checked on a recording bus (`benchvista test`). Not yet run on a real display.
