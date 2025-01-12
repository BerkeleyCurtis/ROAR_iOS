import logging
import websocket
from typing import List, Optional, Tuple, List
import cv2
import numpy as np
from pathlib import Path
from ROAR.utilities_module.module import Module

import datetime


class RGBCamStreamer(Module):
    def save(self, **kwargs):
        pass

    def __init__(self, host, port, resize: Optional[Tuple] = None,
                 name: str = "world_cam", threaded: bool = True,
                 update_interval: float = 0.5,
                 has_intrinsics: bool = True,
                 is_ar:bool = False):
        super().__init__(threaded=threaded, name=name, update_interval=update_interval)

        self.logger = logging.getLogger(f"{self.name} server on [{host}:{port}]")
        self.host = host
        self.port = port
        self.ws = websocket.WebSocket()
        self.intrinsics: Optional[np.ndarray] = None
        self.resize = resize
        self.has_intrinsics = has_intrinsics
        self.is_ar = is_ar

        self.curr_image: Optional[np.ndarray] = None
        self.logger.info(f"{name} initialized")

    def connect(self):
        for i in range(10):
            try:
                self.ws.connect(f"ws://{self.host}:{self.port}/{self.name}", timeout=0.1)
            except Exception as e:
                self.logger.error(e)
    def receive(self):
        try:
            img = self.ws.recv()
            if self.has_intrinsics:
                intrinsics_str = self.ws.recv()
            try:
                img = np.frombuffer(img, dtype=np.uint8)
                self.curr_image = cv2.imdecode(img, cv2.IMREAD_UNCHANGED)[:, :, :3]
                if self.has_intrinsics:
                    intrinsics_arr = [float(i) for i in intrinsics_str.split(",")]
                    self.intrinsics = np.array([
                        [intrinsics_arr[0], 0, intrinsics_arr[2]],
                        [0, intrinsics_arr[1], intrinsics_arr[3]],
                        [0, 0, 1]
                    ])
            except Exception as e:
                self.logger.error(f"Failed to decode image: {e}")
        except Exception as e:
            # self.logger.error(f"Failed to get image: {e}")
            self.curr_image = None
            pass

    def run_in_series(self, **kwargs):
        self.receive()


if __name__ == '__main__':
    ir_image_server = RGBCamStreamer(host="10.142.143.48", port=8005, name="world_cam")
    ir_image_server.run_in_series()
