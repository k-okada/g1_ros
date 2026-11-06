#!/usr/bin/env python3
"""
Run gear_sonic's PICO manager with ROS 2 instead of its own ZMQ PUB socket.

pico_manager_thread_server.run_pico_manager() is imported and run unmodified. Only its
output is redirected:

  - the ZMQ PUB socket it binds on the manager port (--port, default 5556) is never bound;
    its messages are converted to ROS instead, so the port stays free for
    gear_sonic_interface (the only process talking to the deploy stack). Other PUB sockets
    (e.g. --optical-hand-port with the optical-hands patch) stay real ZMQ;
  - "pose" messages (POSE mode, full-body SMPL) are published as
    gear_sonic_interfaces/SmplMotion on <prefix>/smpl_motion (newest frame only;
    gear_sonic_interface rebuilds the sliding window itself);
  - "command" messages (mode switches from the PICO buttons) become service calls:
      start, planner=True   -> enable_smpl_stream(false) if streaming, else enable_control(true)
      start, planner=False  -> enable_smpl_stream(true)  (enable_control(true) first if needed)
      stop                  -> enable_smpl_stream(false) if streaming, then enable_control(false)
    stop does NOT put the robot into damping like the original manager (see
    gear_sonic_interface OnEnableControl); the deploy reverts to IDLE balance.
  - "planner" messages (PLANNER / PLANNER_VR_3PT modes) and "manager_state" are dropped:
    while in a planner mode the robot is driven by gear_sonic_interface (IDLE unless
    /cmd_vel etc. are published).

The deploy feedback reader (g1_debug SUB, port 5557) still uses real ZMQ.

Usage (needs a Python that can import both rclpy and gear_sonic's teleop deps):
  ros2 run g1_pico_teleop pico_manager_ros.py --gr00t_root ~/GR00T-WholeBodyControl [--ros-args ...]
"""

import argparse
import importlib
import inspect
import os
import sys
import threading
import types

from gear_sonic_interfaces.msg import SmplMotion
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
from std_srvs.srv import SetBool


class PicoManagerRos(Node):
    """Receives pico_manager's outgoing messages and forwards them to gear_sonic_interface."""

    def __init__(self, prefix, service_timeout):
        super().__init__("pico_manager_ros")
        self.prefix = prefix.rstrip("/")
        self.service_timeout = service_timeout
        self.pub = self.create_publisher(SmplMotion, f"{self.prefix}/smpl_motion", 10)
        self.enable_control_cli = self.create_client(SetBool, f"{self.prefix}/enable_control")
        self.enable_smpl_cli = self.create_client(SetBool, f"{self.prefix}/enable_smpl_stream")
        self.control_active = False
        self.streaming_smpl = False

    # ── pose ────────────────────────────────────────────────────────────────
    def publish_pose(self, data):
        i = -1  # newest frame; pico sends a window of num_frames_to_send frames
        msg = SmplMotion()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.frame_index = int(data["frame_index"][i])
        msg.smpl_pose = data["smpl_pose"][i].reshape(-1).astype(float).tolist()
        msg.smpl_joints = data["smpl_joints"][i].reshape(-1).astype(float).tolist()
        q = data["body_quat_w"][i]  # scalar-first (w, x, y, z)
        msg.body_quat.w = float(q[0])
        msg.body_quat.x = float(q[1])
        msg.body_quat.y = float(q[2])
        msg.body_quat.z = float(q[3])
        msg.joint_pos = data["joint_pos"][i].reshape(-1).astype(float).tolist()
        msg.joint_vel = data["joint_vel"][i].reshape(-1).astype(float).tolist()
        self.pub.publish(msg)

    # ── command ─────────────────────────────────────────────────────────────
    def _call(self, client, value):
        """Blocking service call; the executor spins in another thread."""
        if not client.wait_for_service(timeout_sec=self.service_timeout):
            self.get_logger().error(f"{client.srv_name} not available")
            return False
        done = threading.Event()
        future = client.call_async(SetBool.Request(data=value))
        future.add_done_callback(lambda _: done.set())
        if not done.wait(self.service_timeout):
            self.get_logger().error(f"{client.srv_name}({value}) timed out")
            return False
        res = future.result()
        if not res.success:
            self.get_logger().error(f"{client.srv_name}({value}) failed: {res.message}")
        return res.success

    def handle_command(self, start, stop, planner):
        if stop:
            if self.streaming_smpl:
                self._call(self.enable_smpl_cli, False)
                self.streaming_smpl = False
            self._call(self.enable_control_cli, False)
            self.control_active = False
        elif start and planner:
            if self.streaming_smpl:
                # enable_smpl_stream(false) also returns to planner control
                self._call(self.enable_smpl_cli, False)
                self.streaming_smpl = False
                self.control_active = True
            elif not self.control_active:
                self.control_active = self._call(self.enable_control_cli, True)
        elif start and not planner:
            if not self.control_active:
                # stand up / balance before switching to streamed motion
                self.control_active = self._call(self.enable_control_cli, True)
            if self.control_active and not self.streaming_smpl:
                self.streaming_smpl = self._call(self.enable_smpl_cli, True)


class _Msg:
    """Tagged message object returned by the patched builders instead of ZMQ bytes."""

    def __init__(self, kind, **kwargs):
        self.kind = kind
        self.kwargs = kwargs


class _PubSocket:
    """Wraps pico_manager's zmq.PUB sockets.

    Only the socket bound to the manager port (default 5556, the SONIC deploy input) is
    redirected to ROS; any other PUB socket (e.g. the optical-hand publisher added by
    patches/pico_manager_optical_hands.patch) keeps working as a real ZMQ socket.
    """

    def __init__(self, real_socket, node, manager_port):
        self._real = real_socket
        self._node = node
        self._manager_port = manager_port
        self._to_ros = False

    def bind(self, addr, *args, **kwargs):
        if str(addr).rsplit(":", 1)[-1] == str(self._manager_port):
            self._to_ros = True
            return None
        return self._real.bind(addr, *args, **kwargs)

    def send(self, msg, *args, **kwargs):
        if not self._to_ros:
            return self._real.send(msg, *args, **kwargs)
        if isinstance(msg, _Msg):
            if msg.kind == "pose":
                self._node.publish_pose(msg.kwargs["data"])
            elif msg.kind == "command":
                self._node.handle_command(**msg.kwargs)
            # "planner" / "manager_state": dropped
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _patch_pico_manager(pm, node, manager_port):
    real_zmq = pm.zmq

    class _Context:
        def __init__(self, *args, **kwargs):
            self._real = real_zmq.Context(*args, **kwargs)

        def socket(self, socket_type, *args, **kwargs):
            sock = self._real.socket(socket_type, *args, **kwargs)
            if socket_type == real_zmq.PUB:
                return _PubSocket(sock, node, manager_port)
            return sock

        def __getattr__(self, name):
            return getattr(self._real, name)

    zmq_shim = types.SimpleNamespace(**{k: getattr(real_zmq, k) for k in dir(real_zmq)})
    zmq_shim.Context = _Context
    pm.zmq = zmq_shim

    # Builders return tagged objects instead of packed bytes. Only messages sent through the
    # redirected manager socket use these, so real ZMQ sockets never see a _Msg.
    def pack_pose_message(pose_data, topic="pose", version=3):
        return _Msg("pose", data=pose_data) if topic == "pose" else _Msg(topic)

    def build_command_message(start=False, stop=False, planner=False, **_kwargs):
        return _Msg("command", start=bool(start), stop=bool(stop), planner=bool(planner))

    def build_planner_message(*_args, **_kwargs):
        return _Msg("planner")

    pm.pack_pose_message = pack_pose_message
    pm.build_command_message = build_command_message
    pm.build_planner_message = build_planner_message


def main():
    argv = remove_ros_args(sys.argv)[1:]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--gr00t_root",
        default=os.environ.get("GR00T_WBC_DIR", os.path.expanduser("~/GR00T-WholeBodyControl")),
        help="GR00T-WholeBodyControl checkout (default: $GR00T_WBC_DIR or ~/GR00T-WholeBodyControl)",
    )
    parser.add_argument("--prefix", default="/gear_sonic_interface")
    parser.add_argument(
        "--port",
        type=int,
        default=5556,
        help="pico_manager PUB port to redirect to ROS (not actually bound)",
    )
    parser.add_argument(
        "--optical-hand-port",
        type=int,
        default=0,
        help="Passed to a pico_manager patched with patches/pico_manager_optical_hands.patch",
    )
    parser.add_argument("--service_timeout", type=float, default=3.0)
    parser.add_argument("--input_source", default="xrt")
    parser.add_argument("--buffer_size", type=int, default=15)
    parser.add_argument("--num_frames_to_send", type=int, default=5)
    parser.add_argument("--target_fps", type=int, default=50)
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--record_dir", default="")
    parser.add_argument("--zmq_feedback_host", default="localhost")
    parser.add_argument("--zmq_feedback_port", type=int, default=5557)
    parser.add_argument("--vis_vr3pt", action="store_true")
    parser.add_argument("--vis_smpl", action="store_true")
    parser.add_argument("--waist_tracking", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.path.abspath(os.path.expanduser(args.gr00t_root)))
    pm = importlib.import_module("gear_sonic.scripts.pico_manager_thread_server")

    rclpy.init(args=sys.argv)
    node = PicoManagerRos(args.prefix, args.service_timeout)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    _patch_pico_manager(pm, node, args.port)
    extra = {}
    if args.optical_hand_port:
        if "optical_hand_port" not in inspect.signature(pm.run_pico_manager).parameters:
            parser.error("--optical-hand-port needs the optical-hands patched pico_manager")
        extra["optical_hand_port"] = args.optical_hand_port
    try:
        pm.run_pico_manager(
            port=args.port,
            buffer_size=args.buffer_size,
            num_frames_to_send=args.num_frames_to_send,
            target_fps=args.target_fps,
            use_cuda=args.cuda,
            record_dir=args.record_dir,
            zmq_feedback_host=args.zmq_feedback_host,
            zmq_feedback_port=args.zmq_feedback_port,
            enable_vis_vr3pt=args.vis_vr3pt,
            with_g1_robot=True,
            enable_waist_tracking=args.waist_tracking,
            enable_smpl_vis=args.vis_smpl,
            input_source=args.input_source,
            **extra,
        )
    except SystemExit:
        pass  # run_pico_manager calls exit() on the OFF (A+B+X+Y) transition
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
