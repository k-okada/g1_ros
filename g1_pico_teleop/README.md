# PICO Full-Body Teleoperation

Start body control with the PICO controller buttons. Enable optical hand control with a workstation command. You can start the hand adapter first.

## Start the robot programs | Terminal A

Connect the workstation to the `unitree-g1-nx` Wi-Fi. Secure the robot with the support harness as shown in the tutorial, with both feet on the ground.

Open terminal A on the workstation and log in to the robot:

```bash
ssh unitree@unitree-g1-nx.local
```

After logging in, run these commands in the same terminal:

```bash
sudo systemctl start ros2-g1-gear-sonic-bringup.service
cd ~/GR00T-WholeBodyControl
.venv_teleop/bin/python -B gear_sonic/scripts/pico_manager_thread_server.py --manager --optical-hand-port 5560
```

Keep terminal A open. These commands start SONIC, ROS, the Inspire driver, and XRoboToolkit PC Service.

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

If body control is active, pick up the controllers and **press A+B+X+Y together, then release** to stop it. The manager then exits. Do not press this combination if body control has not started: it will start it.

Press `Ctrl+C` in **workstation terminal B** to exit the hand adapter.

Stop the services in **robot terminal A**:

```bash
sudo systemctl stop ros2-g1-gear-sonic-bringup.service gear-sonic.service inspire-g1.service
```

If the manager is still running, press `Ctrl+C` in terminal A before running the service stop command.

## Current data flow

```text
PICO → G1 XRoboToolkit → manager
                          ├─ ZMQ 5556 → SONIC → Body
                          └─ ZMQ 5560 → Workstation hand adapter
                                      → JointTrajectory → G1 ros2_control → Inspire hands
```

Body control goes directly through the original manager, without ROS `SmplMotion`. The local package is already built; use the commands above to start it.

The only modified robot file is `~/GR00T-WholeBodyControl/gear_sonic/scripts/pico_manager_thread_server.py`. Omit `--optical-hand-port 5560` to use the original controller workflow. The original file is saved on the workstation desktop. See the [patch](patches/pico_manager_optical_hands.patch).

## Alternative: body control through ROS (`pico_manager_ros.py`)

`scripts/pico_manager_ros.py` imports the unmodified `pico_manager_thread_server.run_pico_manager()` and redirects only its manager PUB socket (`--port`, default 5556) to ROS. That port is never bound, so `gear_sonic_interface` (`use_gear_sonic:=true`) stays the only process talking to SONIC:

- POSE-mode SMPL frames → `gear_sonic_interfaces/SmplMotion` on `/gear_sonic_interface/smpl_motion` (newest frame per message)
- PICO button mode switches → `/gear_sonic_interface/enable_control` / `enable_smpl_stream`. A+B+X+Y returns to IDLE balance instead of damping.
- PLANNER / VR-3PT planner messages are dropped. In those modes the robot is driven by `gear_sonic_interface`.
- Other PUB sockets, such as `--optical-hand-port` with the optical-hands patch, and the 5557 feedback SUB stay real ZMQ.

It needs one Python that imports rclpy (Jazzy: 3.12) and the gear_sonic teleop dependencies (`xrobotoolkit_sdk`, torch, ...):

```bash
source /opt/ros/jazzy/setup.bash && source install/setup.bash
export ROS_DOMAIN_ID=164
ros2 run g1_pico_teleop pico_manager_ros.py --gr00t_root ~/GR00T-WholeBodyControl
```
