import sys
if sys.prefix == '/usr':
    sys.real_prefix = sys.prefix
    sys.prefix = sys.exec_prefix = '/home/yolo/Desktop/Ros2/Dofbot2/install/record_dataset_cr5'
