> **This repository has moved.** Active development continues in [barlowa124/lab-informatics](https://github.com/barlowa124/lab-informatics) under [`lab_instrument_gateway/`](https://github.com/barlowa124/lab-informatics/tree/main/lab_instrument_gateway). This repo is archived and kept for link stability.

---

# lablink - instrument gateway

Software that talks to a lab instrument, captures its readings, and serves them over an API.

No hardware is required: the device is an emulator (`lablink/simulator.py`) that speaks an
ASCII, SCPI-flavored command set over TCP. The driver code path is the same one a physical
RS-232 or TCP instrument would use: open a socket, send newline-terminated commands, parse
one-line responses, poll the error register, reconnect when the link dies. Swapping the
emulator for a real instrument means changing the transport, not the driver.

## layout

- `lablink/simulator.py` - emulated benchtop bioreactor. Temperature and agitation ramp to
  setpoints, pH drifts, dissolved oxygen responds to agitation, weight drains. Fault modes
  (`SIM:FAULT STUCK|NOISY|DROP`) exist so tests can exercise driver failure handling.
- `lablink/driver.py` - `InstrumentClient`: connect/reconnect, `query`/`command`,
  `MEAS:<ch>?` readings, `CONF:<ch> <value>` setpoints, `SYST:ERR?` error register.
  One transaction at a time on the shared socket so the capture loop and API calls
  cannot interleave frames.
- `lablink/capture.py` - polling service. Each channel is read on an interval, validated
  into a typed `ReadingRow`, persisted to SQLite, and checked against alarm rules.
  Transport and device errors are stored as rows too. A silent gap in instrument data is
  worse than an honest error row.
- `lablink/api.py` - FastAPI: `/api/instrument`, `/api/latest`, `/api/history/{ch}`,
  `/api/alarms`, `/api/setpoint/{ch}`, plus a small live dashboard at `/`.
  Setpoint writes reach the instrument, so they require a bearer token.
- `dashboard/index.html` - dependency-free status page.

## run it

```bash
pip install -e .
python -m lablink.demo          # emulator on :5025, API+dashboard on :8000
# open http://127.0.0.1:8000
```

The demo prints a bearer token at startup. Setpoint writes need it:

```bash
curl -X POST -H 'Authorization: Bearer <token>' 'http://127.0.0.1:8000/api/setpoint/TEMP?value=37.5'
```

Read endpoints stay open. Set `LABLINK_API_TOKEN` to use your own token, or
`LABLINK_ALLOWED_ORIGINS` (comma-separated) to permit extra browser origins.
Requests carrying an `Origin` header that does not match the server's host are
rejected, and without a token configured `create_app` refuses writes unless it
is built with `allow_insecure_writes=True`, which is only safe on loopback.

Inject a fault while it runs:

```bash
printf 'SIM:FAULT DROP\n' | nc 127.0.0.1 5025   # watch quality rows turn transport-error
printf 'SIM:FAULT NONE\n' | nc 127.0.0.1 5025   # driver reconnects on its own
```

## protocol

| command | response | meaning |
|---|---|---|
| `*IDN?` | `LABLINK,THRIVE-1000,BIOREACTOR,1.4.2` | identity |
| `MEAS:TEMP?` | `36.982` | measure a channel (TEMP, PH, DO, AGIT, WEIGHT) |
| `CONF:TEMP 37.5` | `OK` | move a setpoint |
| `SYST:ERR?` | `0,"No error"` | pop the error register (destructive read) |
| `STAT?` | `RUNNING` | run state |
| `RUN` / `STOP` | `OK` | start/stop the process |
| `SIM:FAULT <mode>` | `OK` | test-only fault injection |

Errors are returned as `-<code>,"<message>"` and surface as `InstrumentError`.

The driver retries idempotent commands after a transport failure, since a lost
reply can mean the command ran twice anyway. `SYST:ERR?` is different: a
successful read consumes the register, so if its reply is lost the driver
raises `TransportError` instead of retrying into a cleared register. Non-finite
readings (`nan`, `inf`) are rejected at the driver and stored as device-error
rows, never as valid data.

## tests

```bash
pip install -e .[dev]
python -m pytest tests/
```

27 tests cover protocol round-trips, setpoint validation, measurement drift, link-drop
reconnect, negative-value parsing, concurrent transaction safety, error-register reads
(including the no-retry-on-lost-reply rule), non-finite rejection, setpoint
authentication and origin checks, error-row persistence, alarm firing, shutdown
ordering, and the API surface.

## honest scope

This is instrument software development without the instrument. It demonstrates the
parts that carry over to real hardware - wire protocols, timeouts, reconnects, error
registers, typed capture, alarms, provenance - and not the parts that do not, like
electrical noise, connector quirks, or vendor SDK licensing. The emulator's process
model is a first-order approximation, not a validated bioreactor model.

## Related work

- [dockops](https://github.com/barlowa124/dockops) applies the same provenance-manifest discipline to docking pipelines instead of instrument captures.
- [bioprocess-decision-runtime](https://github.com/barlowa124/bioprocess-decision-runtime) consumes this repo's captures: `python -m bioprocess_runtime capture-scenario capture.sqlite` evaluates the newest readings window through its advisory-only policy.
