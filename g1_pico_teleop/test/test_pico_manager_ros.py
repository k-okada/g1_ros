import importlib.util
from pathlib import Path
import sys
import types

import numpy as np
import pytest

pytest.importorskip("rclpy")
pytest.importorskip("gear_sonic_interfaces.msg")
zmq = pytest.importorskip("zmq")


def load_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "pico_manager_ros.py"
    spec = importlib.util.spec_from_file_location("test_pico_manager_ros", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RecordingNode:
    def __init__(self):
        self.poses = []
        self.commands = []

    def publish_pose(self, data):
        self.poses.append(data)

    def handle_command(self, **kwargs):
        self.commands.append(kwargs)


def make_fake_pico_manager():
    """Mimics the parts of pico_manager_thread_server that pico_manager_ros relies on."""
    pm = types.ModuleType("fake_pico_manager")
    pm.zmq = zmq

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("must be patched")

    pm.pack_pose_message = unavailable
    pm.build_command_message = unavailable
    pm.build_planner_message = unavailable

    def run(port, other_port):
        context = pm.zmq.Context()
        manager = context.socket(pm.zmq.PUB)
        manager.bind(f"tcp://127.0.0.1:{port}")
        other = context.socket(pm.zmq.PUB)
        other.bind(f"tcp://127.0.0.1:{other_port}")
        manager.send(pm.build_planner_message(0, [0.0, 0.0, 0.0]))
        manager.send(pm.build_command_message(start=True, stop=False, planner=False))
        manager.send(pm.pack_pose_message({"frame_index": np.arange(5)}, topic="pose"))
        manager.send(pm.pack_pose_message({}, topic="manager_state"))
        other.send(b"raw")
        manager.close()
        other.close()
        context.term()

    pm.run = run
    return pm


def free_port():
    with zmq.Context() as context, context.socket(zmq.PUB) as sock:
        return sock.bind_to_random_port("tcp://127.0.0.1")


def test_manager_port_is_redirected_and_other_pub_sockets_stay_real():
    module = load_script()
    pm = make_fake_pico_manager()
    node = RecordingNode()
    manager_port = free_port()
    module._patch_pico_manager(pm, node, manager_port)

    pm.run(manager_port, free_port())

    assert node.commands == [{"start": True, "stop": False, "planner": False}]
    assert len(node.poses) == 1
    assert list(node.poses[0]["frame_index"]) == [0, 1, 2, 3, 4]
    # The manager port was never bound: gear_sonic_interface can own it.
    with zmq.Context() as context, context.socket(zmq.PUB) as sock:
        sock.bind(f"tcp://127.0.0.1:{manager_port}")


class Bridge:
    """PicoManagerRos.handle_command without a ROS graph: _call is recorded."""

    def __init__(self, module, results=None):
        self.bridge = module.PicoManagerRos.__new__(module.PicoManagerRos)
        self.bridge.enable_control_cli = "enable_control"
        self.bridge.enable_smpl_cli = "enable_smpl_stream"
        self.bridge.control_active = False
        self.bridge.streaming_smpl = False
        self.calls = []
        self.results = results or {}

        def call(client, value):
            self.calls.append((client, value))
            return self.results.get((client, value), True)

        self.bridge._call = call

    def command(self, start, stop, planner):
        self.bridge.handle_command(start=start, stop=stop, planner=planner)


def test_command_sequence_maps_to_gear_sonic_services():
    bridge = Bridge(load_script())

    bridge.command(start=True, stop=False, planner=True)  # OFF -> PLANNER
    bridge.command(start=True, stop=False, planner=False)  # PLANNER -> POSE
    bridge.command(start=True, stop=False, planner=True)  # POSE -> PLANNER
    bridge.command(start=True, stop=False, planner=False)  # PLANNER -> POSE
    bridge.command(start=False, stop=True, planner=True)  # -> OFF

    assert bridge.calls == [
        ("enable_control", True),
        ("enable_smpl_stream", True),
        ("enable_smpl_stream", False),
        ("enable_smpl_stream", True),
        ("enable_smpl_stream", False),
        ("enable_control", False),
    ]


def test_pose_without_balance_enables_control_first_and_stops_on_failure():
    bridge = Bridge(load_script(), results={("enable_control", True): False})

    bridge.command(start=True, stop=False, planner=False)

    assert bridge.calls == [("enable_control", True)]
    assert not bridge.bridge.streaming_smpl
