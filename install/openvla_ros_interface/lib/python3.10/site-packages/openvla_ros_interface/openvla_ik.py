import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import Image, JointState
from geometry_msgs.msg import PoseStamped
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from moveit_msgs.srv import GetPositionIK
from moveit_msgs.msg import PositionIKRequest, RobotState
from tf_transformations import quaternion_from_euler, euler_from_quaternion
from tf2_ros import Buffer, TransformListener
from builtin_interfaces.msg import Duration

import requests
import base64
import cv2
from cv_bridge import CvBridge

class OpenVLAIKPlanner(Node):
    def __init__(self):
        super().__init__('openvla_ik_planner')

        self.PLANNING_GROUP_ARM = "cr5_group"
        self.PLANNING_FRAME = "base_link"
        self.EE_LINK = "Link6"
        self.processing = False
        self.bridge = CvBridge()
        
        # IK service client
        self.ik_client = self.create_client(GetPositionIK, '/compute_ik')
        
        # Joint trajectory publisher
        self.trajectory_pub = self.create_publisher(
            JointTrajectory, 
            '/cr5_group_controller/joint_trajectory', 
            10
        )
        
        # Image subscription
        self.sub = self.create_subscription(
            Image, 
            'camera_link/image_raw', 
            self.callback, 
            10
        )

        # Joint state subscription
        self.current_joint_state = None
        self.joint_state_sub = self.create_subscription(
            JointState, 
            'joint_states', 
            self.joint_state_callback, 
            10
        )

        # TF setup
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # OpenVLA API
        self.api_url = "https://639e20c24026.ngrok-free.app/predict"
        
        # Joint names (matching controller configuration)
        self.joint_names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        
        self.get_logger().info("OpenVLA IK Planner initialized")

    def get_current_pose(self):
        try:
            now = rclpy.time.Time()
            trans = self.tf_buffer.lookup_transform(
                self.PLANNING_FRAME,
                self.EE_LINK,
                now,
                timeout=rclpy.duration.Duration(seconds=1.0)
            )

            pose = PoseStamped()
            pose.header.stamp = self.get_clock().now().to_msg()
            pose.header.frame_id = self.PLANNING_FRAME
            pose.pose.position.x = trans.transform.translation.x
            pose.pose.position.y = trans.transform.translation.y
            pose.pose.position.z = trans.transform.translation.z
            pose.pose.orientation = trans.transform.rotation

            return pose
        except Exception as e:
            self.get_logger().warn(f"TF lookup failed: {e}")
            return None

    def callback(self, msg):
        if self.processing:
            return
        self.processing = True
        
        try:
            instruction = "pick red cube and place on green one"

            # Convert image and get OpenVLA prediction
            cv_image = self.bridge.imgmsg_to_cv2(msg, "bgr8")
            _, buffer = cv2.imencode('.jpg', cv_image)
            b64_image = base64.b64encode(buffer).decode('utf-8')

            response = requests.post(self.api_url, json={
                "image": b64_image,
                "instruction": instruction
            })
            delta = response.json()["action"]
            self.get_logger().info(f"OpenVLA predicted delta: {delta}")

            # Get current pose
            current_pose = self.get_current_pose()
            if current_pose is None:
                self.get_logger().error("Current end-effector pose not available")
                self.processing = False
                return

            # Calculate target pose
            target_pose = PoseStamped()
            target_pose.header.frame_id = self.PLANNING_FRAME
            target_pose.header.stamp = self.get_clock().now().to_msg()

            # Add position deltas
            target_pose.pose.position.x = current_pose.pose.position.x + delta[0]
            target_pose.pose.position.y = current_pose.pose.position.y + delta[1]
            target_pose.pose.position.z = current_pose.pose.position.z + delta[2]

            # Add orientation deltas to current orientation
            current_q = current_pose.pose.orientation
            roll, pitch, yaw = euler_from_quaternion([
                current_q.x, current_q.y, current_q.z, current_q.w
            ])
            new_q = quaternion_from_euler(
                roll + delta[3],
                pitch + delta[4],
                yaw + delta[5]
            )
            target_pose.pose.orientation.x = new_q[0]
            target_pose.pose.orientation.y = new_q[1]
            target_pose.pose.orientation.z = new_q[2]
            target_pose.pose.orientation.w = new_q[3]

            # Solve IK and publish trajectory
            self.solve_ik_and_publish(target_pose)

        except Exception as e:
            self.get_logger().error(f"Callback error: {str(e)}")
        finally:
            self.processing = False

    def solve_ik_and_publish(self, target_pose):
        if not self.ik_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("IK service not available")
            return

        if self.current_joint_state is None:
            self.get_logger().error("Current joint state not available")
            return

        # Create IK request
        ik_request = PositionIKRequest()
        ik_request.group_name = self.PLANNING_GROUP_ARM
        ik_request.pose_stamped = target_pose
        ik_request.timeout = Duration(sec=5)

        # Set robot state (current joint positions as seed)
        robot_state = RobotState()
        robot_state.joint_state = self.current_joint_state
        ik_request.robot_state = robot_state

        # Create service request
        request = GetPositionIK.Request()
        request.ik_request = ik_request

        # Call IK service
        self.get_logger().info("Calling IK service...")
        future = self.ik_client.call_async(request)
        
        def ik_response_callback(future):
            try:
                response = future.result()
                if response.error_code.val == 1:  # SUCCESS
                    self.get_logger().info("IK solution found")
                    self.publish_joint_trajectory(response.solution.joint_state)
                else:
                    self.get_logger().error(f"IK failed with error code: {response.error_code.val}")
            except Exception as e:
                self.get_logger().error(f"IK service call failed: {e}")
        
        future.add_done_callback(ik_response_callback)

    def publish_joint_trajectory(self, joint_state):
        # Debug: Print IK solution joint names
        self.get_logger().info(f"IK solution joint names: {joint_state.name}")
        self.get_logger().info(f"Expected joint names: {self.joint_names}")
        
        # Create joint trajectory message
        trajectory = JointTrajectory()
        trajectory.header.stamp = self.get_clock().now().to_msg()
        trajectory.joint_names = self.joint_names

        # Create trajectory point
        point = JointTrajectoryPoint()
        
        # Map joint positions from solution to trajectory
        joint_positions = [0.0] * len(self.joint_names)
        
        for i, joint_name in enumerate(self.joint_names):
            if joint_name in joint_state.name:
                idx = joint_state.name.index(joint_name)
                joint_positions[i] = joint_state.position[idx]
                self.get_logger().info(f"Mapped {joint_name}: {joint_state.position[idx]}")
            else:
                self.get_logger().warn(f"Joint {joint_name} not found in IK solution")
        
        point.positions = joint_positions
        point.velocities = [0.0] * len(self.joint_names)  # Stop at target
        point.accelerations = [0.0] * len(self.joint_names)
        
        # Set time to reach target (adjust as needed)
        point.time_from_start = Duration(sec=0, nanosec=200_000_000)
        
        trajectory.points = [point]

        # Publish trajectory
        self.trajectory_pub.publish(trajectory)
        self.get_logger().info(f"Published joint trajectory: {joint_positions}")

    def joint_state_callback(self, msg):
        self.current_joint_state = msg

def main(args=None):
    rclpy.init(args=args)
    node = OpenVLAIKPlanner()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Shutting down")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()