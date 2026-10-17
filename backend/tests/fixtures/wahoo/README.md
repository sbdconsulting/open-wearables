# Wahoo FIT fixture

`elemnt_bolt.fit` is a real 2-minute ride recorded on a Wahoo ELEMNT BOLT
(firmware 196.28), copied unchanged from python-fitparse's test suite
(`tests/files/elemnt-bolt-no-application-id-inside-developer-data-id.fit`,
https://github.com/dtcooper/python-fitparse, MIT licence).

It is real rather than built with `tests/fixtures/fit_builder.py` on purpose: the
point of the Wahoo tests that use it is to pin how a real Wahoo device names
itself, which a synthetic file could only assume. Measured contents:

- `file_id`: manufacturer `wahoo_fitness`, product `31`.
- `device_info` with `device_index="creator"`: `product_name="ELEMNT BOLT"`,
  `descriptor="ELEMNT BOLT 143C"` (serial suffix), `software_version=196.28`.
  The other `device_info` rows are sensors (speed sensor, GPS, temperature).
- `session`: start 2017-08-21T08:18:00Z, 140 s elapsed, 963.65 m,
  avg/max power 182/665 W, max speed 10.596 m/s, no heart rate.
- 132 `record` messages (power, cadence, speed, GPS, altitude, temperature).
