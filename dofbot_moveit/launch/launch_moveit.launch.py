from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration
from ament_index_python.packages import get_package_share_directory
from moveit_configs_utils import MoveItConfigsBuilder
import os

def generate_launch_description():
    # Declare launch argument for simulation time
    is_sim = LaunchConfiguration("is_sim")
    is_sim_arg = DeclareLaunchArgument(
        "is_sim",
        default_value="True",
        description="Use simulation time"
    )

    # Package directories
    moveit_pkg = get_package_share_directory("dofbot_moveit")
    description_pkg = get_package_share_directory("dofbot_description")

    # Build MoveIt configuration
    moveit_config = (
        MoveItConfigsBuilder("dofbot_description", package_name="dofbot_moveit")
        .robot_description(file_path=os.path.join(description_pkg, "urdf", "new_cr5.xacro"))
        .robot_description_semantic(file_path=os.path.join(moveit_pkg, "config", "cr5_robot.srdf"))
        .trajectory_execution(file_path=os.path.join(moveit_pkg, "config", "moveit_controllers.yaml"))
        .to_moveit_configs()
    )

    # Launch Move Group node
    move_group_node = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=[
            moveit_config.to_dict(),
            {"use_sim_time": is_sim},
            {"publish_robot_description_semantic": True}
        ],
        arguments=["--ros-args", "--log-level", "error"]
    )

    # RViz configuration file
    rviz_config = os.path.join(moveit_pkg, "config", "moveit.rviz")

    # Launch RViz node
    rviz_node = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        output="log",
        arguments=["-d", rviz_config],
        parameters=[
            moveit_config.robot_description,
            moveit_config.robot_description_semantic,
            moveit_config.robot_description_kinematics,
            moveit_config.joint_limits,
        ]
    )

    return LaunchDescription([
        is_sim_arg,
        move_group_node,
        rviz_node
    ])
