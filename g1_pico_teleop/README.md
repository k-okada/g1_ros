# PICO Full-Body Teleoperation

Start body control with the PICO controller buttons. Enable optical hand control with a workstation command. You can start the hand adapter first.

The robot runs the bringup with `use_gear_sonic:=true`, so `gear_sonic_interface` owns ZMQ port 5556 to SONIC. Body commands from PICO therefore go through ROS: `pico_manager_ros.py` runs the original PICO manager and sends its output to `gear_sonic_interface`. See [Body control through ROS](#body-control-through-ros-pico_manager_rospy) for details.

> [!NOTE]
> `pico_manager_ros.py` has only been tested against a stub manager and mock services. It has not been run with a real PICO or SONIC yet.

## Prerequisites

`pico_manager_ros.py` needs one Python that can import both rclpy (ROS 2 Jazzy, Python 3.12) and the gear_sonic teleop dependencies (`xrobotoolkit_sdk`, torch, ...). The `.venv_teleop` created by `install_pico.sh` uses a different Python version, so prepare a Python 3.12 environment with the teleop dependencies on the robot first. The robot host runs ROS inside the `ghcr.io/mqcmd196/g1_ros` container, so one option is to add the teleop dependencies to that image.

`--optical-hand-port` requires `~/GR00T-WholeBodyControl/gear_sonic/scripts/pico_manager_thread_server.py` on the robot to have the [optical-hands patch](patches/pico_manager_optical_hands.patch) applied.

## Start the robot programs | Terminal A

Connect the workstation to the `unitree-g1-nx` Wi-Fi. Secure the robot with the support harness as shown in the tutorial, with both feet on the ground.

Open terminal A on the workstation and log in to the robot:

```bash
ssh unitree@unitree-g1-nx.local
```

After logging in, start SONIC, ROS (with `use_gear_sonic:=true`) and the Inspire driver:

```bash
sudo systemctl start ros2-g1-gear-sonic-bringup.service
```

Then start the PICO manager in the same terminal, in the Python 3.12 environment from [Prerequisites](#prerequisites):

```bash
source /opt/ros/jazzy/setup.bash && source <g1_ros workspace>/install/setup.bash
source <python 3.12 teleop venv>/bin/activate
export ROS_DOMAIN_ID=164
export FASTDDS_BUILTIN_TRANSPORTS=LARGE_DATA
ros2 run g1_pico_teleop pico_manager_ros.py --gr00t_root ~/GR00T-WholeBodyControl --optical-hand-port 5560
```

Do not run `pico_manager_thread_server.py` directly: it would try to bind port 5556, which `gear_sonic_interface` already uses.

Keep terminal A open. The manager also starts XRoboToolkit PC Service.

## Connect and calibrate | PICO

Connect PICO to the same Wi-Fi and open XRoboToolkit:

- Set the PC Service IP to `10.42.0.1`.
- Complete body and leg tracker calibration.
- Wait until the robot terminal stops showing `waiting for body data...` and displays `Manager controls`.

This step is required even if you use optical hands first.

## Start the hand adapter | Workstation terminal B

Open another terminal B on the workstation and run:

```bash
cd /home/cici/projects/GrootReproucing/g1_ros
source /opt/ros/jazzy/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=164
export FASTDDS_BUILTIN_TRANSPORTS=LARGE_DATA
ros2 launch g1_pico_teleop pico_hands.launch.py hand_zmq_endpoint:=tcp://10.42.0.1:5560
```

Keep terminal B open. Hand output is disabled at this point; the robot hands will not follow yet.

## Start body control | PICO controllers

Pick up both controllers. Stand upright with your feet together, upper arms against your sides, elbows bent 90 degrees, and palms facing each other.

- **Press A+B+X+Y together, then release all four buttons** to start body balance. Terminal A should display `OFF -> PLANNER`.
- Wait for the robot to stabilize and align your arms with its pose. **Press A+X together, then release** to start body following. Terminal A should display `PLANNER -> POSE`.
- Gently put down the controllers so PICO can track your bare hands. Wait for **`left=ok right=ok`** in terminal A before enabling optical hand control.

You do not need to keep holding the controllers. Body following is already active, so the robot may follow your movements as you put them down.

## Enable optical hand control | Workstation terminal C

Open another terminal C on the workstation and run:

```bash
source /opt/ros/jazzy/setup.bash
source /home/cici/projects/GrootReproucing/g1_ros/install/setup.bash
export ROS_DOMAIN_ID=164
export FASTDDS_BUILTIN_TRANSPORTS=LARGE_DATA
ros2 service call /pico_hand_adapter/enable std_srvs/srv/SetBool '{data: true}'
```

The robot hands start following once both hands are tracked and the adapter receives the robot's `/joint_states`. A response of `waiting for fresh paired tracking` means it is still waiting for paired hand data.

## Stop

First, disable hand output in **workstation terminal C**:

```bash
ros2 service call /pico_hand_adapter/enable std_srvs/srv/SetBool '{data: false}'
```

If body control is active, pick up the controllers and **press A+B+X+Y together, then release** to stop it. The robot returns to IDLE balance (it does not go limp) and the manager exits. Do not press this combination if body control has not started: it will start it.

Press `Ctrl+C` in **workstation terminal B** to exit the hand adapter.

Stop the services in **robot terminal A**:

```bash
sudo systemctl stop ros2-g1-gear-sonic-bringup.service gear-sonic.service inspire-g1.service
```

If the manager is still running, press `Ctrl+C` in terminal A before running the service stop command.

## Current data flow

```text
PICO → G1 XRoboToolkit → pico_manager_ros.py (imports the original manager)
                          ├─ ROS SmplMotion / services → gear_sonic_interface → ZMQ 5556 → SONIC → Body
                          └─ ZMQ 5560 → Workstation hand adapter
                                      → JointTrajectory → G1 ros2_control → Inspire hands
```

The only modified robot file is `~/GR00T-WholeBodyControl/gear_sonic/scripts/pico_manager_thread_server.py` (the [optical-hands patch](patches/pico_manager_optical_hands.patch)). The original file is saved on the workstation desktop. `pico_manager_ros.py` imports this file and does not modify it. Omit `--optical-hand-port 5560` to use the controller workflow without optical hands.

## Body control through ROS (`pico_manager_ros.py`)

`scripts/pico_manager_ros.py` imports `pico_manager_thread_server.run_pico_manager()` and runs it unchanged. It keeps the manager's PICO processing and button state machine, and only changes where the output goes:

- The manager's PUB socket on `--port` (default 5556) is never bound. `gear_sonic_interface` is the only process that talks to SONIC.
- POSE-mode SMPL frames are published as `gear_sonic_interfaces/SmplMotion` on `/gear_sonic_interface/smpl_motion`, one newest frame per message.
- Button mode switches become service calls:

  | PICO buttons | Manager transition | ROS service calls |
  |---|---|---|
  | A+B+X+Y | OFF → PLANNER | `enable_control(true)` |
  | A+X | PLANNER → POSE | `enable_smpl_stream(true)` |
  | A+X | POSE → PLANNER | `enable_smpl_stream(false)` |
  | A+B+X+Y | any → OFF (manager exits) | `enable_smpl_stream(false)` if streaming, then `enable_control(false)` |

- Stopping with A+B+X+Y returns the robot to IDLE balance. Unlike the original manager, it does **not** put the robot into damping.
- PLANNER and VR-3PT planner messages are dropped. In those modes the robot is driven by `gear_sonic_interface` (IDLE unless `/cmd_vel` and similar topics are published).
- In POSE mode, joystick heading (`heading_increment`), Dex3 finger joints and `vr_position` are not forwarded, because `SmplMotion` does not carry them.
- Other ZMQ sockets stay real: the optical-hand PUB (`--optical-hand-port`) and the 5557 deploy feedback SUB (`--zmq_feedback_host`, default `localhost`).
