import sys
if sys.prefix == '/usr':
    sys.real_prefix = sys.prefix
    sys.prefix = sys.exec_prefix = '/home/om/OpenVla_cr5/install/openvla_ros_interface'
