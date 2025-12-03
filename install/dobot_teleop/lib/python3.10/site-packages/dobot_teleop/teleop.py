#!/usr/bin/env python3
import rclpy
import time
import math
import requests
import threading
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import Int32MultiArray, Float32MultiArray # <--- ADDED Float32MultiArray
from dobot_msgs_v4.srv import EnableRobot, ServoP, GetPose, ClearError
from tf_transformations import euler_from_quaternion
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

class ArTeleopPlannerReal(Node):
    def __init__(self):
        super().__init__('ar_teleop_planner_real')

        # --- CONFIGURATION ---
        self.ROBOT_IP = "192.168.1.6"  
        self.LIMITS = { 'x': (-800.0, 0.0), 'y': (-500.0, 500.0), 'z': (155.0, 750.0) }
        
        # SAFETY SETTINGS
        self.MAX_JUMP_THRESHOLD = 40.0  
        self.MIN_MOVE_THRESHOLD = 2.0   
        self.CMD_INTERVAL = 0.033  # ~30Hz

        # --- STATE ---
        self.teleop_locked = False   
        self.last_sent_coords = None
        self.current_gripper_val = 1.0 # Default Open (1.0) <--- ADDED STATE TRACKING
        
        # --- ROS SETUP ---
        qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1)

        self.clear_error_client = self.create_client(ClearError, '/dobot_bringup_ros2/srv/ClearError')
        self.enable_robot_client = self.create_client(EnableRobot, '/dobot_bringup_ros2/srv/EnableRobot')
        self.get_pose_client = self.create_client(GetPose, '/dobot_bringup_ros2/srv/GetPose')
        self.servop_client = self.create_client(ServoP, '/dobot_bringup_ros2/srv/ServoP')

        # --- DATA COLLECTION PUBLISHER ---
        # Publishes [x, y, z, rx, ry, rz, gripper]
        self.cmd_pub = self.create_publisher(Float32MultiArray, '/teleop/target_pose', 10) # <--- ADDED

        self.ar_pose_sub = self.create_subscription(PoseStamped, '/bros2/ar_pose', self.ar_pose_callback, qos)
        self.volume_sub = self.create_subscription(Int32MultiArray, '/bros/volume', self.volume_callback, 10)

        self.robot_enabled = False
        self.initialized = False
        self.robot_pose_initialized = False
        self.getting_initial_pose_flag = False
        self.initial_robot_pose = None
        self.initial_phone_pose = None
        
        self.position_scale = 900.0      
        self.rotation_scale = 180.0 / 3.14159265359 

        self.get_logger().info("AR Teleop + Data Gen Starting...")
        self.wait_for_services()
        self.async_clear_error()

    def wait_for_services(self):
        for client in [self.clear_error_client, self.enable_robot_client, self.get_pose_client, self.servop_client]:
            client.wait_for_service()

    # ---------------------------------------------------------------------
    # GRIPPER LOGIC
    # ---------------------------------------------------------------------
    def volume_callback(self, msg):
        if self.teleop_locked: return
        data = list(msg.data)
        if len(data) >= 2:
            if data[0] == 1 and data[1] == 0:
                self.trigger_gripper("close")
            elif data[0] == 0 and data[1] == 1:
                self.trigger_gripper("open")

    def trigger_gripper(self, action):
        self.get_logger().info(f"Triggering: {action}")
        self.teleop_locked = True
        
        # Update State immediately for recorder
        self.current_gripper_val = 0.0 if action == "close" else 1.0 # <--- UPDATE STATE
        
        thread = threading.Thread(target=self._send_gripper_command, args=(action,))
        thread.daemon = True
        thread.start()

    def _send_gripper_command(self, action):
        try:
            time.sleep(0.3) 
            url = f"http://{self.ROBOT_IP}:22000/interface/toolDataExchange"
            headers = {"Content-Type": "application/json"}
            
            if action == "close": payload = {"value": [1, 6, 1, 3, 0, 0, 120, 54]}
            else: payload = {"value": [1, 6, 1, 3, 3, 232, 120, 136]}
            
            requests.post(url, json=payload, headers=headers, timeout=2.0)
        except Exception as e:
            self.get_logger().error(f"Gripper failed: {e}")
        finally:
            time.sleep(1.0) 
            self.last_sent_coords = None 
            self.wake_up_robot() 
            self.teleop_locked = False

    def wake_up_robot(self):
        req = EnableRobot.Request()
        self.enable_robot_client.call_async(req)

    # ---------------------------------------------------------------------
    # TELEOP LOOP
    # ---------------------------------------------------------------------
    def ar_pose_callback(self, msg):
        if not self.robot_enabled: return
        if self.teleop_locked: return 

        current_time = time.time()
        if (current_time - self.last_cmd_time) < self.CMD_INTERVAL: return

        phone_pose = msg.pose
        if not self.initialized:
            self.handle_init(phone_pose)
            return

        dx = (phone_pose.position.x - self.initial_phone_pose.position.x) * self.position_scale
        dy = (phone_pose.position.y - self.initial_phone_pose.position.y) * self.position_scale
        dz = (phone_pose.position.z - self.initial_phone_pose.position.z) * self.position_scale

        tx = self.initial_robot_pose['x'] - dz
        ty = self.initial_robot_pose['y'] - dx
        tz = self.initial_robot_pose['z'] + dy

        tx_safe = max(self.LIMITS['x'][0], min(tx, self.LIMITS['x'][1]))
        ty_safe = max(self.LIMITS['y'][0], min(ty, self.LIMITS['y'][1]))
        tz_safe = max(self.LIMITS['z'][0], min(tz, self.LIMITS['z'][1]))

        ph_e = euler_from_quaternion([phone_pose.orientation.x, phone_pose.orientation.y, phone_pose.orientation.z, phone_pose.orientation.w])
        
        rx = self.initial_robot_euler[0] + (ph_e[1] - self.initial_phone_euler[1]) * self.rotation_scale
        ry = self.initial_robot_euler[1] + (ph_e[2] - self.initial_phone_euler[2]) * self.rotation_scale
        rz = self.initial_robot_euler[2] + (ph_e[0] - self.initial_phone_euler[0]) * self.rotation_scale

        if self.last_sent_coords:
            dist = math.sqrt((tx_safe - self.last_sent_coords[0])**2 + (ty_safe - self.last_sent_coords[1])**2 + (tz_safe - self.last_sent_coords[2])**2)
            if dist > self.MAX_JUMP_THRESHOLD or dist < self.MIN_MOVE_THRESHOLD: return

        # 1. SEND COMMAND TO ROBOT
        self.send_servop(tx_safe, ty_safe, tz_safe, rx, ry, rz)
        
        # 2. PUBLISH TARGET FOR RECORDER <--- ADDED
        # Msg format: [x, y, z, rx, ry, rz, gripper]
        target_msg = Float32MultiArray()
        target_msg.data = [float(tx_safe), float(ty_safe), float(tz_safe), float(rx), float(ry), float(rz), self.current_gripper_val]
        self.cmd_pub.publish(target_msg)

        self.last_sent_coords = (tx_safe, ty_safe, tz_safe)
        self.last_cmd_time = current_time

    def send_servop(self, x, y, z, rx, ry, rz):
        req = ServoP.Request()
        req.a, req.b, req.c = float(x), float(y), float(z)
        req.d, req.e, req.f = float(rx), float(ry), float(rz)
        self.servop_client.call_async(req)

    def handle_init(self, phone_pose):
        if not self.robot_pose_initialized:
            if not self.getting_initial_pose_flag:
                self.getting_initial_pose_flag = True
                self.async_get_initial_pose()
            return
        self.initial_phone_pose = phone_pose
        self.initial_phone_euler = euler_from_quaternion([phone_pose.orientation.x, phone_pose.orientation.y, phone_pose.orientation.z, phone_pose.orientation.w])
        self.initialized = True
        self.get_logger().info("Teleop Initialized.")

    def async_clear_error(self):
        self.clear_error_client.call_async(ClearError.Request()).add_done_callback(self.clear_cb)
    def clear_cb(self, f):
        if f.result().res == 0: self.async_enable_robot()
        else: self.create_timer(1.0, self.async_clear_error)

    def async_enable_robot(self):
        self.enable_robot_client.call_async(EnableRobot.Request()).add_done_callback(self.enable_cb)
    def enable_cb(self, f):
        if f.result().res == 0: self.robot_enabled = True; self.get_logger().info("Robot Enabled.")
        else: self.create_timer(1.0, self.async_enable_robot)

    def async_get_initial_pose(self):
        self.get_pose_client.call_async(GetPose.Request()).add_done_callback(self.pose_cb)
    def pose_cb(self, f):
        try:
            p = f.result().robot_return.strip().replace('{','').replace('}','').split(',')
            self.initial_robot_pose = {'x':float(p[0]),'y':float(p[1]),'z':float(p[2])}
            self.initial_robot_euler = (float(p[3]),float(p[4]),float(p[5]))
            self.robot_pose_initialized = True
        except: self.getting_initial_pose_flag = False

def main(args=None):
    rclpy.init(args=args)
    node = ArTeleopPlannerReal()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    try: executor.spin()
    except KeyboardInterrupt: pass
    finally: node.destroy_node(); rclpy.shutdown()

if __name__ == "__main__": main()