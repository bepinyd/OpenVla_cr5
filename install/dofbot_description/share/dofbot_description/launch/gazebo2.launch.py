import os
from pathlib import Path
from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, SetEnvironmentVariable, TimerAction
from launch.substitutions import Command, LaunchConfiguration
from launch.launch_description_sources import PythonLaunchDescriptionSource

from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    robot_description_dir = get_package_share_directory("dofbot_description")

    model_arg = DeclareLaunchArgument(
        name="model",
        default_value=os.path.join(robot_description_dir, "urdf", "new_cr5.xacro"),
        description="Absolute path to robot urdf file"
    )

    gazebo_resource_path = SetEnvironmentVariable(
        name="GZ_SIM_RESOURCE_PATH",
        value=[str(Path(robot_description_dir).parent.resolve())]
    )

    ros_distro = os.environ["ROS_DISTRO"]
    is_ignition = "True" if ros_distro == "humble" else "False"

    robot_description = ParameterValue(
        Command([
            "xacro ",
            LaunchConfiguration("model"),
            " is_ignition:=",
            is_ignition
        ]),
        value_type=str
    )

    robot_state_publisher_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        parameters=[{"robot_description": robot_description, "use_sim_time": True}]
    )

    world_path = os.path.join(robot_description_dir, "worlds", "openvla_test.world")

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            [os.path.join(get_package_share_directory("ros_gz_sim"), "launch", "gz_sim.launch.py")]
        ),
        launch_arguments={
            "gz_args": f"-v 4 -r {world_path}"
        }.items()
    )

    # Spawn robot entity - delayed to ensure Gazebo is ready
    gz_spawn_entity = TimerAction(
        period=3.0,
        actions=[
            Node(
                package="ros_gz_sim",
                executable="create",
                output="screen",
                arguments=["-topic", "robot_description", "-name", "dofbot",
                            '-x', '0', '-y', '-0.6', '-z', '0.74',
                            '-R', '0', '-P', '0', '-Y', '1.5708']
            )
        ]
    )

    # ROS 2 Control Node - REQUIRED and must be uncommented
    ros2_control_node = TimerAction(
        period=4.0,
        actions=[
            Node(
                package="controller_manager",
                executable="ros2_control_node",
                parameters=[
                    {"robot_description": robot_description},
                    {"use_sim_time": True}
                ],
                output="screen"
            )
        ]
    )

    # Bridge for communication between Gazebo and ROS 2
    start_gazebo_ros_bridge_cmd = Node(
        package='ros_gz_bridge',
        executable='parameter_bridge',
        arguments=[
            "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock",
            "/cr5_group_controller/joint_trajectory@trajectory_msgs/msg/JointTrajectory[gz.msgs.JointTrajectory",
            # Camera topics - corrected syntax
            "/camera/image_raw@sensor_msgs/msg/Image[gz.msgs.Image",
            "/camera/camera_info@sensor_msgs/msg/CameraInfo[gz.msgs.CameraInfo",
            # # Robot-mounted RDT2 camera
            # "/umi/image_raw@sensor_msgs/msg/Image[gz.msgs.Image",
            # "/umi/camera_info@sensor_msgs/msg/CameraInfo[gz.msgs.CameraInfo"
        ],
        output='screen',
    )

    ros_gz_image_bridge = Node(
        package="ros_gz_image",
        executable="image_bridge",
        arguments=["/camera/image_raw"]
    )

    ros_gz_umi_camera_bridge = Node(
    package="ros_gz_image",
    executable="image_bridge",
    arguments=["/umi/image_raw"]
     )

    # Controller spawners - delayed to ensure ros2_control_node is ready
    joint_state_broadcaster_spawner = TimerAction(
        period=6.0,
        actions=[
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=[
                    "joint_state_broadcaster",
                    "--controller-manager",
                    "/controller_manager",
                ],
                output="screen"
            )
        ]
    )

    cr5_group_controller = TimerAction(
        period=7.0,
        actions=[
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=[
                    "cr5_group_controller",
                    "--controller-manager",
                    "/controller_manager"
                ],
                output="screen"
            )
        ]
    )

    gripper_controller = TimerAction(
        period=8.0,
        actions=[
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=[
                    "gripper_controller",
                    "--controller-manager",
                    "/controller_manager"
                ],
                output="screen"
            )
        ]
    )

    return LaunchDescription([
        model_arg,
        gazebo_resource_path,
        robot_state_publisher_node,
        gazebo,
        start_gazebo_ros_bridge_cmd,
        ros_gz_image_bridge,
        gz_spawn_entity,
        # ros2_control_node,  # CRITICAL: This must be included
        # joint_state_broadcaster_spawner,
        # cr5_group_controller,
        # gripper_controller,
        # ros_gz_umi_camera_bridge
    ])