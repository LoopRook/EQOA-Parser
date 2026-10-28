"""Unit tests for ClientSocketSource."""
import json
import socket
import time
from eqoa_parser.net.sources import ClientSocketSource
from eqoa_parser.events import Event, KIND_DAMAGE, KIND_HEAL
from eqoa_parser.sources import HEARTBEAT

def test_client_socket_source():
    # Use a non-default test port
    port = 28099
    source = ClientSocketSource(port=port, poll_interval=0.1)
    assert source.has_game_traffic is True
    assert "Native Client" in source.pine_status()

    # Create a sender socket
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # We will iterate in a generator fashion
    it = iter(source)

    # Helper to get next real Event, skipping HEARTBEAT
    def next_event():
        for _ in range(20):
            ts, item = next(it)
            if item != HEARTBEAT:
                return ts, item
        raise TimeoutError("No event received")

    # Send a damage event
    payload = {
        "ts": 123456.789,
        "actor": "you",
        "target": "A Moss Snake",
        "amount": 42,
        "ability": "melee",
        "kind": "damage"
    }
    sender.sendto(json.dumps(payload).encode("utf-8"), ("127.0.0.1", port))

    ts, item = next_event()
    assert isinstance(item, Event)
    assert item.actor == "you"
    assert item.target == "A Moss Snake"
    assert item.amount == 42
    assert item.kind == KIND_DAMAGE

    # Send a heal event
    heal_payload = {
        "ts": 123457.000,
        "actor": "Healer",
        "target": "you",
        "amount": 100,
        "ability": "healing",
        "kind": "heal"
    }
    sender.sendto(json.dumps(heal_payload).encode("utf-8"), ("127.0.0.1", port))

    ts, item = next_event()
    assert isinstance(item, Event)
    assert item.actor == "Healer"
    assert item.target == "you"
    assert item.amount == 100
    assert item.kind == KIND_HEAL

    source.close()
    sender.close()
