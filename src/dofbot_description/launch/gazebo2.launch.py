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

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            [os.path.join(get_package_share_directory("ros_gz_sim"), "launch", "gz_sim.launch.py")]
        ),
        launch_arguments={
            "gz_args": "-v 4 -r empty.sdf"
        }.items()
    )

    gz_spawn_entity = Node(
        package="ros_gz_sim",
        executable="create",
        output="screen",
        arguments=["-topic", "robot_description", "-name", "dofbot"]
    )


    ros2_control_node=Node(
        package="controller_manager",
        executable="ros2_control_node",
        arguments=[
            "--controller-manager",
            "/controller_manager",
        ],
    )

    joint_state_broadcaster_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=[
            "joint_state_broadcaster",
            "--controller-manager",
            "/controller_manager",
        ],
    )

    cr5_group_controller = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["cr5_group_controller",
                "--controller-manager",
                "/controller_manager"
        ]
    )

    gripper_controller=Node(
        package="controller_manager",
        executable="spawner",
        arguments=["gripper_controller",
                "--controller-manager",
                "/controller_manager"
        ]
    )

 

    start_gazebo_ros_bridge_cmd = Node(
    package='ros_gz_bridge',
    executable='parameter_bridge',
    arguments=[
      "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock",
      "/cr5_group_controller/joint_trajectory@trajectory_msgs/msg/JointTrajectory[gz.msgs.JointTrajectory"
    ],
    output='screen',
)
    # bridge_params = os.path.join(get_package_share_directory('dofbot_description'),'config','gz_bridge.yaml')
    # ros_gz_bridge = Node(
    #     package="ros_gz_bridge",
    #     executable="parameter_bridge",
    #     arguments=[
    #         '--ros-args',
    #         '-p',
    #         f'config_file:={bridge_params}',
    #     ]
    # )




    return LaunchDescription([
        model_arg,
        gazebo_resource_path,
        robot_state_publisher_node,
        gazebo,
        start_gazebo_ros_bridge_cmd,
        gz_spawn_entity,
        # ros2_control_node,
        joint_state_broadcaster_spawner,
        cr5_group_controller,
        
        # gripper_controller
    ])