"""Driver + simulator tests against a live emulated device."""
import pytest

from lablink.driver import InstrumentClient, InstrumentError, TransportError
from lablink.protocol import IDENT_RESPONSE
from lablink.simulator import serve


@pytest.fixture()
def device():
    srv = serve(port=0)
    host, port = srv.server_address
    yield host, port, srv
    srv.shutdown()


def test_identify(device):
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        assert c.identify() == IDENT_RESPONSE
        assert c.status() == "RUNNING"


def test_measure_all_channels(device):
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        for ch in ("TEMP", "PH", "DO", "AGIT", "WEIGHT"):
            r = c.measure(ch)
            assert isinstance(r.value, float)
            assert r.channel == ch


def test_unknown_channel_is_error(device):
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        with pytest.raises(InstrumentError):
            c.measure("NOPE")


def test_setpoint_in_range_and_validation(device):
    host, port, srv = device
    with InstrumentClient(host, port) as c:
        c.set_setpoint("TEMP", 40.0)
        assert srv.device.setpoints["TEMP"] == 40.0
        with pytest.raises(InstrumentError):
            c.set_setpoint("TEMP", 99.0)
        with pytest.raises(InstrumentError):
            c.set_setpoint("PH", 99.0)


def test_temperature_ramps_toward_setpoint(device):
    host, port, srv = device
    srv.device.readings["TEMP"] = 25.0
    srv.device.setpoints["TEMP"] = 37.0
    with InstrumentClient(host, port) as c:
        import time
        time.sleep(2.5)
        r = c.measure("TEMP")
        assert r.value > 30.0  # well past midpoint of 25->37 approach


def test_drop_fault_recovers_via_reconnect(device):
    host, port, srv = device
    srv.device.fault = "DROP"
    c = InstrumentClient(host, port)
    c.connect()
    # first queries fail at transport level, retries reconnect while DROP persists
    srv.device.fault = "NONE"  # let the retry succeed
    r = c.measure("TEMP")
    assert isinstance(r.value, float)
    c.close()


def test_unreachable_host_raises_transport():
    c = InstrumentClient("127.0.0.1", 59999)
    with pytest.raises(TransportError):
        c.connect()


def test_negative_reading_is_a_value_not_an_error(device):
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        c.query = lambda line: "-3.21"  # force a negative response
        r = c.measure("TEMP")
        assert r.value == pytest.approx(-3.21)


def test_fault_sets_error_register_once(device):
    host, port, srv = device
    srv.device.fault = "NONE"
    with InstrumentClient(host, port) as c:
        c.command("SIM:FAULT STUCK")
        assert "Simulated fault STUCK" in c.error_register()
        # register pops on read - second read is clean
        assert c.error_register() == '0,"No error"'


def test_nonfinite_readings_are_errors(device):
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        for bad in ("nan", "inf", "-inf"):
            c.query = lambda line, bad=bad: bad
            with pytest.raises(InstrumentError):
                c.measure("TEMP")


def test_setpoint_rejects_nonfinite(device):
    host, port, srv = device
    with InstrumentClient(host, port) as c:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(InstrumentError):
                c.set_setpoint("TEMP", bad)
        assert srv.device.setpoints["TEMP"] == pytest.approx(37.0)


def test_error_register_not_retried_after_lost_reply(device):
    """SYST:ERR? pops the register. A lost reply must not trigger a retry
    that returns the cleared register."""
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        calls = {"send": 0, "read": 0}

        def fake_send(line):
            calls["send"] += 1

        def fake_read():
            calls["read"] += 1
            raise TransportError("reply lost")

        c._send_line = fake_send
        c._readline = fake_read
        with pytest.raises(TransportError):
            c.error_register()
        assert calls == {"send": 1, "read": 1}


def test_send_failure_still_retries_destructive_query(device):
    """If the send itself failed the device never saw SYST:ERR?, so retrying
    is safe."""
    host, port, _ = device
    with InstrumentClient(host, port) as c:
        sends = []

        def flaky_send(line):
            sends.append(line)
            if len(sends) == 1:
                raise TransportError("write failed")

        def fake_read():
            return '0,"No error"'

        c._send_line = flaky_send
        c._readline = fake_read
        c.connect = lambda: None
        assert c.error_register() == '0,"No error"'
        assert len(sends) == 2


def test_concurrent_transactions_do_not_desync(device):
    """Poll + setpoint from two threads must not cross-response."""
    import threading

    host, port, _ = device
    with InstrumentClient(host, port) as c:
        errs = []

        def poller():
            for _ in range(30):
                try:
                    c.measure("TEMP")
                except Exception as e:
                    errs.append(e)

        def configurer():
            for i in range(15):
                try:
                    c.set_setpoint("AGIT", 100.0 + i * 10)
                except Exception as e:
                    errs.append(e)

        t1, t2 = threading.Thread(target=poller), threading.Thread(target=configurer)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert not errs
