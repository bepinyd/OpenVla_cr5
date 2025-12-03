#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import cv2
import os
import json
import signal
import sys
import shutil
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sensor_msgs.msg import JointState
from dobot_msgs_v4.msg import ToolVectorActual
from std_msgs.msg import Float32MultiArray

###########################################
# CONFIGURATION
###########################################
DATA_ROOT = "dataset_repo"  # The folder where all data will go
CHUNK = "chunk-000"         # Data chunk (keep as is)
FPS = 20                    # Recording FPS
CAM_ID_FRONT = 0            # /dev/video0 (Top-Corner View)
CAM_ID_WRIST = 2            # /dev/video2 (Wrist View)
WIDTH, HEIGHT = 640, 480    # Keep 640x480 for USB bandwidth stability

class EpisodeRecorder(Node):
    def __init__(self, episode_index, task_index, task_description):
        
        super().__init__("episode_recorder")
        self.episode_index = episode_index
        self.task_index = task_index
        self.task_description = task_description
        self.start_time = self.get_clock().now().nanoseconds

        # -- BUFFERS --
        self.latest_joints = None
        self.latest_tool = None
        self.latest_target = None # From Teleop: [x,y,z,rx,ry,rz,gripper]
        self.records = []
        
        # -- CAMERA SETUP --
        print(f"[INIT] Opening Cameras {CAM_ID_FRONT} & {CAM_ID_WRIST}...")
        self.cap_front = cv2.VideoCapture(CAM_ID_FRONT)
        self.cap_wrist = cv2.VideoCapture(CAM_ID_WRIST)
        
        # Optimize Cams
        for cap in [self.cap_front, self.cap_wrist]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, FPS)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) # Lower latency

        # -- VIDEO WRITERS --
        # Folder structure: videos/chunk-X/observation.images.key/
        self.path_front = os.path.join(DATA_ROOT, "videos", CHUNK, "observation.images.front")
        self.path_wrist = os.path.join(DATA_ROOT, "videos", CHUNK, "observation.images.wrist")
        os.makedirs(self.path_front, exist_ok=True)
        os.makedirs(self.path_wrist, exist_ok=True)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        vid_name = f"episode_{episode_index:06d}.mp4"
        self.out_front = cv2.VideoWriter(os.path.join(self.path_front, vid_name), fourcc, FPS, (WIDTH, HEIGHT))
        self.out_wrist = cv2.VideoWriter(os.path.join(self.path_wrist, vid_name), fourcc, FPS, (WIDTH, HEIGHT))

        # -- ROS SUBSCRIBERS --
        self.create_subscription(JointState, "/joint_states_robot", self.cb_joint, 10)
        self.create_subscription(ToolVectorActual, "/dobot_msgs_v4/msg/ToolVectorActual", self.cb_tool, 10)
        self.create_subscription(Float32MultiArray, "/teleop/target_pose", self.cb_target, 10)

        # -- RECORDING LOOP --
        self.create_timer(1.0/FPS, self.loop)
        print(f"[REC] Recording Episode {episode_index} | Task: {task_description}")

    def cb_joint(self, msg): self.latest_joints = msg
    def cb_tool(self, msg): self.latest_tool = msg
    def cb_target(self, msg): self.latest_target = msg.data # [x,y,z,rx,ry,rz,gripper]

    def loop(self):
        # 1. READ CAMERAS
        ret1, frame1 = self.cap_front.read()
        ret2, frame2 = self.cap_wrist.read()

        # If cams fail, skip frame (or handle error)
        if ret1: self.out_front.write(frame1)
        if ret2: self.out_wrist.write(frame2)

        # 2. CHECK ROBOT DATA
        if self.latest_joints is None or self.latest_tool is None:
            return # Wait for ROS connection

        # 3. CALCULATE ACTION
        # Action = Target_Pose - Current_Pose
        if self.latest_target is None:
            # No command yet -> Action is 0
            action_delta = [0.0] * 6
            gripper_action = 1.0 # Default Open
        else:
            # Current TCP (Ensure this is TOOL FRAME in DobotStudio!)
            curr_pos = [self.latest_tool.x, self.latest_tool.y, self.latest_tool.z, 
                        self.latest_tool.rx, self.latest_tool.ry, self.latest_tool.rz]
            
            # Target TCP
            tgt_pos = self.latest_target[:6]
            
            # Delta Calculation
            action_delta = [t - c for t, c in zip(tgt_pos, curr_pos)]
            gripper_action = self.latest_target[6] # Absolute: 0.0 or 1.0

        # 4. CREATE RECORD (LeRobot Schema)
        record = {
            # --- OBSERVATIONS ---
            "observation.state": list(self.latest_joints.position) + \
                                 [self.latest_tool.x, self.latest_tool.y, self.latest_tool.z,
                                  self.latest_tool.rx, self.latest_tool.ry, self.latest_tool.rz] + \
                                 [gripper_action], 
            
            # --- ACTIONS ---
            # 6 Deltas + 1 Absolute Gripper
            "action": action_delta + [gripper_action], 

            # --- METADATA ---
            "timestamp": float(self.get_clock().now().nanoseconds - self.start_time) / 1e9,
            "episode_index": self.episode_index,
            "index": len(self.records),
            "task_index": self.task_index,
            "annotation.human.validity": 1, # Default to Valid
            "next.done": False,
            "next.reward": 0.0
        }
        self.records.append(record)

    def shutdown(self):
        print("\n[STOP] Finalizing Episode...")
        
        # Close Cams
        self.cap_front.release()
        self.cap_wrist.release()
        self.out_front.release()
        self.out_wrist.release()

        # Save Parquet
        p_path = os.path.join(DATA_ROOT, "data", CHUNK, f"episode_{self.episode_index:06d}.parquet")
        os.makedirs(os.path.dirname(p_path), exist_ok=True)
        
        if len(self.records) > 0:
            pq.write_table(pa.Table.from_pylist(self.records), p_path)
            print(f"[SAVE] Parquet saved at: {p_path}")
            
            # Update Meta
            meta_entry = {
                "episode_index": self.episode_index,
                "tasks": [self.task_description], # Human readable
                "length": len(self.records)
            }
            with open(os.path.join(DATA_ROOT, "meta", "episodes.jsonl"), "a") as f:
                f.write(json.dumps(meta_entry) + "\n")
            print("[SAVE] Metadata updated.")
        else:
            print("[WARN] No records collected. Skipping save.")


# --- METADATA HELPERS ---
def ensure_metadata_files():
    """Generates modality.json and info.json required by GR00T."""
    meta_dir = os.path.join(DATA_ROOT, "meta")
    os.makedirs(meta_dir, exist_ok=True)

    # 1. info.json
    if not os.path.exists(os.path.join(meta_dir, "info.json")):
        info = {"codebase_version": "v2.0", "robot_type": "dobot_cr5", "fps": FPS}
        with open(os.path.join(meta_dir, "info.json"), "w") as f: json.dump(info, f, indent=4)

    # 2. modality.json (CRITICAL)
    if not os.path.exists(os.path.join(meta_dir, "modality.json")):
        modality = {
            "state": {
                "joint_pos": {"start": 0, "end": 6},
                "eef_pos":   {"start": 6, "end": 12},
                "gripper":   {"start": 12, "end": 13}
            },
            "action": {
                "eef_delta":   {"start": 0, "end": 6, "absolute": False}, # Delta
                "gripper_abs": {"start": 6, "end": 7, "absolute": True}   # Absolute
            },
            "video": {
                "front": {"original_key": "observation.images.front"},
                "wrist": {"original_key": "observation.images.wrist"}
            },
            "annotation": {
                "human.validity": {}, 
                "human.action.task_description": {}
            }
        }
        with open(os.path.join(meta_dir, "modality.json"), "w") as f: json.dump(modality, f, indent=4)

def get_task_id(task_desc):
    """Manages tasks.jsonl"""
    t_path = os.path.join(DATA_ROOT, "meta", "tasks.jsonl")
    tasks = {}
    
    # Load existing
    if os.path.exists(t_path):
        with open(t_path, "r") as f:
            for line in f:
                try: d = json.loads(line); tasks[d["task"]] = d["task_index"]
                except: pass
    
    # Return or Create
    if task_desc in tasks: 
        return tasks[task_desc]
    
    new_id = max(tasks.values()) + 1 if tasks else 0
    with open(t_path, "a") as f:
        f.write(json.dumps({"task_index": new_id, "task": task_desc}) + "\n")
        # Ensure 'valid' tag exists too
        if "valid" not in tasks:
            f.write(json.dumps({"task_index": new_id+1, "task": "valid"}) + "\n")
            
    return new_id


def main():
    rclpy.init()
    ensure_metadata_files()
    
    # 1. User Input
    print("\n--- NEW RECORDING SESSION ---")
    task_desc = input("Enter Task Description (e.g., 'pick red cube'): ").strip()
    if not task_desc: task_desc = "pick up object"
    
    task_id = get_task_id(task_desc)
    
    # 2. Find Next Episode Index
    chunk_p = os.path.join(DATA_ROOT, "data", CHUNK)
    if os.path.exists(chunk_p):
        existing = [f for f in os.listdir(chunk_p) if f.endswith(".parquet")]
        # Extract number from episode_00000X.parquet
        nums = [int(f.split("_")[1].split(".")[0]) for f in existing]
        next_ep = max(nums) + 1 if nums else 0
    else:
        next_ep = 0

    # 3. Wait for Ready
    input(f"\n[READY] Task: '{task_desc}' | Episode ID: {next_ep}\n>>> Press ENTER to START recording (Ctrl+C to Stop)...")
    
    # 4. Run Node
    rec = EpisodeRecorder(next_ep, task_id, task_desc)
    try: rclpy.spin(rec)
    except KeyboardInterrupt: rec.shutdown()
    finally: rclpy.shutdown()

if __name__ == "__main__": main()