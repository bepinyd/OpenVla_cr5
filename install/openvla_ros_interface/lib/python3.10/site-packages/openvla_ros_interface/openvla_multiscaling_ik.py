import os
import io
import base64
import requests
import cv2 
import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from rclpy.action import ActionClient
from sensor_msgs.msg import Image, JointState
from geometry_msgs.msg import PoseStamped
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from moveit_msgs.srv import GetPositionIK
from moveit_msgs.msg import PositionIKRequest, RobotState
from control_msgs.action import FollowJointTrajectory
from tf_transformations import quaternion_from_euler, euler_from_quaternion
from tf2_ros import Buffer, TransformListener
from builtin_interfaces.msg import Duration
from cv_bridge import CvBridge
from PIL import Image as PILImage


class OpenVLAIKPlanner(Node):
    def __init__(self):
        super().__init__('openvla_ik_planner')

        self.PLANNING_GROUP_ARM = "arm"
        self.PLANNING_FRAME = "base_link"
        self.EE_LINK = "Link6"
        self.processing = False
        self.bridge = CvBridge()
        
        # IK service client
        self.ik_client = self.create_client(GetPositionIK, '/compute_ik')
        
        # Action client for joint trajectory execution
        self.traj_client = ActionClient(
            self,
            FollowJointTrajectory,
            '/cr5_group_controller/follow_joint_trajectory'
        )

        # Image subscription
        self.sub = self.create_subscription(
            Image, 'camera/image_raw', self.callback, 2
        )

        # Joint state subscription
        self.current_joint_state = None
        self.joint_state_sub = self.create_subscription(
            JointState, 'joint_states', self.joint_state_callback, 2
        )

        # TF setup
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # OpenVLA API
        self.api_url = "https://88a2d8e71465.ngrok-free.app/predict"
        
        # Joint names (matching controller configuration)
        self.joint_names = [
            'joint1', 'joint2', 'joint3',
            'joint4', 'joint5', 'joint6'
        ]
        
        # Scaling setup
        self.scale_levels = [2.4, 2.0, 1.0]   # adaptive scaling sequence
        self.current_scale_idx = 0            # start with 6.0
        self.scale_pos = self.scale_levels[self.current_scale_idx]
        self.scale_rot = 1.0                  # keep orientation scaling fixed

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
            instruction = "Pick up the red cube"

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

            # Apply scaling
            delta[0] *= self.scale_pos
            delta[1] *= self.scale_pos
            delta[2] *= self.scale_pos
            delta[3] *= self.scale_rot
            delta[4] *= self.scale_rot
            delta[5] *= self.scale_rot

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

            # Position update
            target_pose.pose.position.x = current_pose.pose.position.x + delta[0]
            target_pose.pose.position.y = current_pose.pose.position.y + delta[1]
            target_pose.pose.position.z = current_pose.pose.position.z + delta[2]

            # Orientation update
            cq = current_pose.pose.orientation
            roll, pitch, yaw = euler_from_quaternion([
                cq.x, cq.y, cq.z, cq.w
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

            # Solve IK and send trajectory
            self.solve_ik_and_send_goal(target_pose)

        except Exception as e:
            self.get_logger().error(f"Callback error: {e}")
            self.processing = False

    def solve_ik_and_send_goal(self, target_pose):
        if not self.ik_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("IK service not available")
            self.processing = False
            return

        if self.current_joint_state is None:
            self.get_logger().error("Current joint state not available")
            self.processing = False
            return

        # Build IK request
        ik_req = PositionIKRequest()
        ik_req.group_name = self.PLANNING_GROUP_ARM
        ik_req.pose_stamped = target_pose
        ik_req.timeout = Duration(sec=5)

        rs = RobotState()
        rs.joint_state = self.current_joint_state
        ik_req.robot_state = rs

        request = GetPositionIK.Request()
        request.ik_request = ik_req

        self.get_logger().info(f"Calling IK service with scale {self.scale_pos}")
        future = self.ik_client.call_async(request)
        future.add_done_callback(self.ik_response_callback)

    def ik_response_callback(self, future):
        try:
            resp = future.result()
            if resp.error_code.val != 1:
                self.get_logger().error(f"IK failed, code {resp.error_code.val}")

                # Reduce scale if possible
                if self.current_scale_idx < len(self.scale_levels) - 1:
                    self.current_scale_idx += 1
                    self.scale_pos = self.scale_levels[self.current_scale_idx]
                    self.get_logger().warn(f"Reducing scale to {self.scale_pos}")
                self.processing = False
                return

            # Extract positions in correct order
            positions = []
            for name in self.joint_names:
                idx = resp.solution.joint_state.name.index(name)
                positions.append(resp.solution.joint_state.position[idx])

            self.get_logger().info("IK solution found, sending trajectory goal")
            self.send_trajectory_goal(positions)

            # Reset scaling to max after success
            self.current_scale_idx = 0
            self.scale_pos = self.scale_levels[self.current_scale_idx]

        except Exception as e:
            self.get_logger().error(f"IK callback error: {e}")
            self.processing = False

    def send_trajectory_goal(self, joint_positions):
        goal_msg = FollowJointTrajectory.Goal()
        traj = JointTrajectory()
        traj.joint_names = self.joint_names

        point = JointTrajectoryPoint()
        point.positions = joint_positions
        point.time_from_start.sec = 2
        traj.points = [point]

        goal_msg.trajectory = traj

        if not self.traj_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Trajectory action server not available")
            self.processing = False
            return

        send_goal = self.traj_client.send_goal_async(goal_msg)
        send_goal.add_done_callback(self.goal_response_callback)

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().error("Trajectory goal rejected")
            self.processing = False
            return

        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self.result_callback)

    def result_callback(self, future):
        result = future.result().result
        if result.error_code == 0:
            self.get_logger().info("Trajectory execution succeeded")
        else:
            self.get_logger().error(f"Trajectory execution failed: {result.error_code}")
        self.processing = False

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
