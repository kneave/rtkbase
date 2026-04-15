# LC29HBS + RTKBase + gpsd + chrony setup notes

**Machine:** racecontrol  
**Receiver:** Quectel LC29HBS  
**Date:** 2026-04-15

## Purpose

These notes describe the working setup for using an LC29HBS as:
- an RTKBase / RTCM source
- a gpsd source for GNSS time
- a chrony source for PPS-disciplined system time

## Components

### Receiver
The LC29HBS is connected on:
- GNSS serial: `/dev/ttyAMA0`
- PPS: `/dev/pps0`

### RTKBase / str2str
RTKBase relays the receiver stream to TCP port `5015` using `str2str_tcp.service`.

### gpsd
gpsd reads:
- the RTKBase relay on TCP port `5015`
- PPS from `/dev/pps0`

### chrony
chrony uses:
- coarse GNSS time from gpsd
- precise second timing from PPS

## Critical fix

**Do not use `localhost` for the gpsd TCP device.**

This failed in practice:

```text
DEVICES="tcp://localhost:5015 /dev/pps0"
```

This worked:

```text
DEVICES="tcp://127.0.0.1:5015 /dev/pps0"
```

This was the key difference between gpsd failing to produce `TPV/SKY/PPS` and gpsd working normally.

## Required receiver output

The receiver stream feeding RTKBase/gpsd should include standard NMEA sentences needed for time/fix interpretation:
- `GGA`
- `RMC`
- `ZDA`

Proprietary `PQT...` messages and RTCM3 may also be present.

## Symptoms of the broken configuration

With `tcp://localhost:5015`:
- gpsd lists the TCP source as a device
- but does not emit useful `TPV/SKY/TOFF`
- chrony shows GPS unusable
- PPS may appear, but without a proper coarse time source it does not complete the timing chain

## Symptoms of the working configuration

With `tcp://127.0.0.1:5015`:
- gpsd emits `TPV`, `SKY`, `GST`, and `PPS`
- chrony can use PPS properly
- the GPS/PPS time path works while RTKBase remains enabled

## Example gpsd config

In `/etc/default/gpsd`:

```bash
DEVICES="tcp://127.0.0.1:5015 /dev/pps0"
GPSD_OPTIONS="-n -b"
USBAUTO="false"
```

Then restart gpsd:

```bash
sudo systemctl restart gpsd
```

## Example verification steps

### Check RTKBase relay
```bash
ss -ltnp | grep 5015
```

### Inspect relay output
```bash
socat - TCP:127.0.0.1:5015 | strings -n 6 | grep --line-buffered -E 'GGA|RMC|ZDA|PQT'
```

### Query gpsd
```bash
python3 /tmp/gpsd_query_local.py
```

or use a gpsd client on port `2947`.

### Check chrony
```bash
chronyc sources -v
chronyc sourcestats -v
chronyc tracking
```

## Practical takeaway

If RTKBase is the primary purpose of the device, you do **not** need to bypass RTKBase and give gpsd direct serial access, as long as gpsd is configured to read:

```text
tcp://127.0.0.1:5015
```

instead of `tcp://localhost:5015`.
