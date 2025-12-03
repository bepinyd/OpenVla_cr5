#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import cv2
import os
import json
import signal
import sys
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime

from sensor_msgs.msg import JointState
from dobot_msgs_v4.msg import ToolVectorActual


###########################################
# CONFIG
###########################################

DATA_ROOT = "./"        # folder where meta/, data/, videos/ will be created
CHUNK = "chunk-000"     # only one chunk for now
FPS = 15                # camera recording fps
CAMERA_ID = 2          # /dev/video0
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480


###########################################
# ROS2 Collector Node
###########################################

class EpisodeRecorder(Node):
    def __init__(self, episode_index, episode_title):

        super().__init__("episode_recorder")

        self.episode_index = episode_index
        self.episode_title = episode_title

        # Buffers
        self.joint_state = None
        self.tool_vector = None
        self.records = []
        self.timestamps = []

        # ROS2 subscribers
        self.create_subscription(JointState, "/joint_states_robot", self.cb_joint, 10)
        self.create_subscription(ToolVectorActual, "/dobot_msgs_v4/msg/ToolVectorActual", self.cb_tool, 10)

        # Camera
        self.cap = cv2.VideoCapture(CAMERA_ID)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, FPS)

        # Video writer
        video_path = os.path.join(DATA_ROOT, "videos", CHUNK, "observation.images.ego_view",
                                  f"episode_{episode_index:06d}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.video_writer = cv2.VideoWriter(video_path, fourcc, FPS, (VIDEO_WIDTH, VIDEO_HEIGHT))

        print(f"[INFO] Recording video to {video_path}")

        # Timer to save data at FPS rate
        self.timer = self.create_timer(1.0 / FPS, self.capture_step)

    def cb_joint(self, msg: JointState):
        self.joint_state = msg

    def cb_tool(self, msg: ToolVectorActual):
        self.tool_vector = msg

    def capture_step(self):
        """Capture camera + ROS topic data every frame."""
        ret, frame = self.cap.read()
        if ret:
            self.video_writer.write(frame)

        if self.joint_state is None or self.tool_vector is None:
            return

        record = {
            "observation.state": [
                *self.joint_state.position,
                self.tool_vector.x,
                self.tool_vector.y,
                self.tool_vector.z,
                self.tool_vector.rx,
                self.tool_vector.ry,
                self.tool_vector.rz
            ],
            "action": [0, 0, 0, 0],  # Placeholder: you can add actual robot action commands
            "timestamp": float(self.get_clock().now().nanoseconds) / 1e9,
            "annotation.human.action.task_description": 0,
            "task_index": 0,
            "annotation.human.validity": 1,
            "episode_index": self.episode_index,
            "index": len(self.records),
            "next.reward": 0,
            "next.done": False
        }

        self.records.append(record)

    def shutdown(self):
        print("\n[INFO] Finalizing episode and saving parquet...")

        # Close camera
        self.cap.release()
        self.video_writer.release()

        # Save parquet file
        parquet_path = os.path.join(DATA_ROOT, "data", CHUNK, f"episode_{self.episode_index:06d}.parquet")

        table = pa.Table.from_pylist(self.records)
        pq.write_table(table, parquet_path)

        print(f"[INFO] Saved parquet: {parquet_path}")

        # Update episodes.jsonl
        ep_meta_path = os.path.join(DATA_ROOT, "meta", "episodes.jsonl")
        length = len(self.records)

        with open(ep_meta_path, "a") as f:
            meta_entry = {
                "episode_index": self.episode_index,
                "tasks": [self.episode_title, "valid"],
                "length": length
            }
            f.write(json.dumps(meta_entry) + "\n")

        print(f"[INFO] Updated meta: {ep_meta_path}")
        print("[INFO] Done.")


###########################################
# SETUP DIRECTORIES
###########################################

def ensure_folder_structure():
    paths = [
        "meta",
        f"videos/{CHUNK}/observation.images.ego_view",
        f"data/{CHUNK}"
    ]
    for p in paths:
        os.makedirs(os.path.join(DATA_ROOT, p), exist_ok=True)


###########################################
# MAIN
###########################################

def main():
    ensure_folder_structure()

    # Ask episode title
    episode_title = input("Enter episode title: ").strip()

    # Determine episode index by counting existing parquet files
    parquet_dir = os.path.join(DATA_ROOT, "data", CHUNK)
    existing = [f for f in os.listdir(parquet_dir) if f.endswith(".parquet")]
    if existing:
        next_index = max(int(f.split("_")[1].split(".")[0]) for f in existing) + 1
    else:
        next_index = 0

    print(f"[INFO] Starting episode index {next_index}")

    # ROS2 init
    rclpy.init()

    recorder = EpisodeRecorder(next_index, episode_title)

    # Ctrl-C handler
    def sigint_handler(sig, frame):
        recorder.shutdown()
        rclpy.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    print("[INFO] Recording... press CTRL+C to stop.")

    try:
        rclpy.spin(recorder)
    except KeyboardInterrupt:
        pass

    recorder.shutdown()
    rclpy.shutdown()


if __name__ == "__main__":
    main()

