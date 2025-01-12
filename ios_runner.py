from ROAR.utilities_module.data_structures_models import SensorsData
from ROAR.utilities_module.vehicle_models import Vehicle, VehicleControl
from ROAR.agent_module.agent import Agent
from ROAR.utilities_module.data_structures_models import Transform
from ROAR_iOS.config_model import iOSConfig
from Bridges.ios_bridge import iOSBridge
import logging
import pygame
from typing import Optional, Tuple
from ROAR_iOS.manual_control import ManualControl
from ROAR_iOS.depth_cam_streamer import DepthCamStreamer
from ROAR_iOS.rgb_camera_streamer import RGBCamStreamer
from ROAR_iOS.transform_streamer import TransformStreamer
from ROAR_iOS.control_streamer import ControlStreamer
import numpy as np
import cv2
import time


class iOSRunner:
    def __init__(self, agent: Agent, ios_config: iOSConfig):
        self.agent = agent
        self.ios_config = ios_config
        self.ios_bridge = iOSBridge()
        self.pygame_display_width = self.ios_config.pygame_display_width
        self.pygame_display_height = self.ios_config.pygame_display_height
        self.logger = logging.getLogger("iOS Runner")
        self.display: Optional[pygame.display] = None
        self.should_display_system_status = self.ios_config.should_display_system_status
        self.controller = ManualControl(ios_config=ios_config)

        self.setup_pygame()
        self.world_cam_streamer = RGBCamStreamer(host=self.ios_config.ios_ip_addr,
                                                 port=self.ios_config.ios_port,
                                                 name=self.ios_config.world_cam_route_name,
                                                 resize=(self.pygame_display_height,
                                                         self.pygame_display_width),
                                                 update_interval=0.025,
                                                 has_intrinsics=True,
                                                 is_ar = self.ios_config.ar_mode
                                                 )
        self.face_cam_streamer = RGBCamStreamer(host=self.ios_config.ios_ip_addr,
                                                port=self.ios_config.ios_port,
                                                name=self.ios_config.face_cam_route_name,
                                                resize=(self.pygame_display_height,
                                                        self.pygame_display_width),
                                                update_interval=0.025,
                                                has_intrinsics=True
                                                )
        self.depth_cam_streamer = DepthCamStreamer(host=self.ios_config.ios_ip_addr,
                                                   port=self.ios_config.ios_port,
                                                   name=self.ios_config.depth_cam_route_name,
                                                   threaded=True,
                                                   update_interval=0.1,
                                                   )
        self.transform_streamer = TransformStreamer(host=self.ios_config.ios_ip_addr,
                                                    port=self.ios_config.ios_port,
                                                    name=self.ios_config.transform_route_name,
                                                    update_interval=0.01)
        self.control_streamer = ControlStreamer(host=self.ios_config.ios_ip_addr,
                                                port=self.ios_config.ios_port,
                                                name=self.ios_config.control_route_name)

        self.front_cam_display_size: Tuple[int, int] = (100, 480)
        self.front_cam_offsets = (self.pygame_display_width // 2 - self.front_cam_display_size[1] // 2, 0)

        s = (self.pygame_display_height, self.pygame_display_width)
        self.green_overlay_pts = [
            np.array([[0, s[0]],  # lower left
                      [30 * s[1] // 100, 60 * s[0] // 100],  # upper left
                      [70 * s[1] // 100, 60 * s[0] // 100],  # upper right
                      [s[1], s[0]]  # lower right
                      ],
                     np.int32).reshape((-1, 1, 2))
        ]
        self.yellow_overlay_pts = [
            np.array([[0, s[0]],
                      [20 * s[1] // 100, 74 * s[0] // 100],  # upper left
                      [80 * s[1] // 100, 74 * s[0] // 100],  # upper right
                      [s[1], s[0]]
                      ],
                     np.int32).reshape((-1, 1, 2))
        ]
        self.red_overlay_pts = [
            np.array([[0, s[0]],  # lower left
                      [10 * s[1] // 100, 88 * s[0] // 100],  # upper left
                      [90 * s[1] // 100, 88 * s[0] // 100],  # upper right
                      [s[1], s[0]]  # lower right
                      ],
                     np.int32)
        ]

        self.last_control_time = time.time()

        # smoothen control
        # TODO optimize this smoothening
        self.should_smoothen_control = False
        self.prev_control = VehicleControl()
        self.steering_smoothen_factor_forward = 100
        self.steering_smoothen_factor_backward = 10
        self.throttle_smoothen_factor = 100

        self.logger.info("iOS Runner Initialized")

    def setup_pygame(self):
        """
        Initiate pygame
        Returns:

        """
        pygame.init()
        pygame.font.init()
        self.display = pygame.display.set_mode((self.pygame_display_width,
                                                self.pygame_display_height))
        self.logger.debug("PyGame initiated")

    def start_game_loop(self, auto_pilot=False):
        self.logger.info("Starting Game loop")
        self.control_streamer.connect()
        if self.ios_config.ar_mode:
            self.world_cam_streamer.connect()
            self.agent.add_threaded_module(self.world_cam_streamer)
        else:
            self.world_cam_streamer.connect()
            self.depth_cam_streamer.connect()
            self.transform_streamer.connect()
            self.agent.add_threaded_module(self.world_cam_streamer)
            self.agent.add_threaded_module(self.depth_cam_streamer)
            self.agent.add_threaded_module(self.transform_streamer)

        try:
            self.agent.start_module_threads()

            clock = pygame.time.Clock()
            should_continue = True
            while should_continue:
                clock.tick_busy_loop(60)
                should_continue, control = self.update_pygame(clock=clock)
                sensor_data, vehicle = self.convert_data()
                agent_control = self.agent.run_step(vehicle=vehicle,
                                                    sensors_data=sensor_data)
                if auto_pilot:
                    control = self.ios_bridge.convert_control_from_agent_to_source(agent_control)

                # since we can change max throttle on the xbox
                self.ios_config.max_throttle = self.controller.max_throttle
                self.ios_config.steering_offset = self.controller.steering_offset
                control.throttle = np.clip(control.throttle, -self.ios_config.max_throttle,
                                           self.ios_config.max_throttle)
                control.steering = np.clip(control.steering + self.ios_config.steering_offset,
                                           -self.ios_config.max_steering,
                                           self.ios_config.max_steering)
                if self.should_smoothen_control:
                    self.smoothen_control(control)
                if self.ios_config.invert_steering:
                    control.steering = -1 * control.steering
                self.control_streamer.send(control)

        except Exception as e:
            self.logger.error(f"Something bad happend {e}")
        finally:
            self.on_finish()

    def smoothen_control(self, control: VehicleControl):
        if abs(control.throttle) > abs(self.prev_control.throttle) and self.prev_control.throttle > 0.15:
            # ensure slower increase, faster decrease. 0.15 barely drives the car
            control.throttle = (self.prev_control.throttle * self.throttle_smoothen_factor + control.throttle) / \
                               (self.throttle_smoothen_factor + 1)
        if abs(control.steering) < abs(self.prev_control.steering):
            # slowly turn back
            control.steering = (self.prev_control.steering * self.steering_smoothen_factor_backward + control.steering) / \
                               (self.steering_smoothen_factor_backward + 1)
        elif abs(control.steering) < abs(self.prev_control.steering):
            control.steering = (self.prev_control.steering * self.steering_smoothen_factor_forward + control.steering) / \
                               (self.steering_smoothen_factor_backward + 1)

        self.prev_control = control
        return control

    def convert_data(self):
        try:
            rear_rgb = None
            # if self.ios_config.ar_mode:
            #     self.world_cam_streamer.receive()
            # else:
            #     self.world_cam_streamer.receive()
            #     self.depth_cam_streamer.receive()
            #     self.transform_streamer.receive()

            if self.ios_config.ar_mode and self.world_cam_streamer.curr_image is not None:
                front_rgb = cv2.rotate(self.world_cam_streamer.curr_image, cv2.ROTATE_90_CLOCKWISE)
            else:
                front_rgb = cv2.rotate(self.world_cam_streamer.curr_image, cv2.ROTATE_90_CLOCKWISE)

            sensor_data: SensorsData = \
                self.ios_bridge.convert_sensor_data_from_source_to_agent(
                    {
                        "front_rgb": front_rgb,
                        "front_depth": self.depth_cam_streamer.curr_image,
                        "rear_rgb": rear_rgb
                    }
                )
            vehicle = self.ios_bridge.convert_vehicle_from_source_to_agent(
                {
                    "transform": self.transform_streamer.transform,
                }
            )
            current_time = time.time()
            diff = current_time - self.last_control_time
            vehicle.velocity.x = (((self.agent.vehicle.transform.location.x - vehicle.transform.location.x) / diff) + vehicle.velocity.x*5) / 6
            vehicle.velocity.y = (((self.agent.vehicle.transform.location.y - vehicle.transform.location.y) / diff) + vehicle.velocity.y*5) / 6
            vehicle.velocity.z = (((self.agent.vehicle.transform.location.z - vehicle.transform.location.z) / diff) + vehicle.velocity.z*5) / 6
            vehicle.control = self.control_streamer.control_tx
            self.last_control_time = current_time
            if self.ios_config.ar_mode is False and self.depth_cam_streamer.intrinsics is not None:
                self.agent.front_depth_camera.intrinsics_matrix = self.depth_cam_streamer.intrinsics @ self.agent.\
                    front_depth_camera.intrinsics_transformation

            return sensor_data, vehicle
        except Exception as e:
            self.logger.error(f"Cannot convert data: {e}")
            return SensorsData(), Vehicle()

    def on_finish(self):
        self.logger.info("Finishing...")
        self.control_streamer.send(VehicleControl())
        self.agent.shutdown_module_threads()

    def update_pygame(self, clock) -> Tuple[bool, VehicleControl]:
        """
        Update the pygame window, including parsing keypress
        Args:
            clock: pygame clock
        Returns:
            bool - whether to continue the game
            VehicleControl - the new VehicleControl cmd by the keyboard
        """
        if self.display is not None:
            frame = self.generate_current_frame(self.agent.front_rgb_camera.data, self.agent.rear_rgb_camera.data)
            if frame is not None:
                frame = self.impose_reference_line(frame)
                frame: np.ndarray = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).swapaxes(0, 1)
                pygame.surfarray.blit_array(self.display, frame)
                pygame.display.flip()
        pygame.display.flip()
        return self.controller.parse_events(clock=clock)

    def generate_current_frame(self,
                               world_cam_data: Optional[np.ndarray] = None,
                               face_cam_data: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        frame: Optional[np.ndarray] = None
        overlay_frame: Optional[np.ndarray] = None
        if world_cam_data is not None:
            s = world_cam_data.shape
            height = 3*s[1]//4
            min_y = s[0] - height - self.controller.vertical_view_offset
            max_y = s[0] - self.controller.vertical_view_offset
            display_view = world_cam_data[min_y:max_y , :]
            frame = cv2.resize(display_view, dsize=(self.pygame_display_width, self.pygame_display_height))

        if face_cam_data is not None:
            overlay_frame = cv2.resize(face_cam_data,
                                       (self.front_cam_display_size[1], self.front_cam_display_size[0]))
        if overlay_frame is not None and frame is not None:
            x_offset = self.front_cam_offsets[0]
            y_offset = self.front_cam_offsets[1]
            frame[y_offset:y_offset + overlay_frame.shape[0],
            x_offset:x_offset + overlay_frame.shape[1]] = overlay_frame
        if self.should_display_system_status:
            self.display_system_status(frame)
        return frame

    def impose_reference_line(self, frame: np.ndarray):
        frame = cv2.polylines(frame, self.green_overlay_pts, isClosed=True, color=(0, 255, 0), thickness=2)
        frame = cv2.polylines(frame, self.yellow_overlay_pts, isClosed=True, color=(0, 255, 255), thickness=2)
        frame = cv2.polylines(frame, self.red_overlay_pts, isClosed=True, color=(0, 0, 255), thickness=2)

        return frame

    def display_system_status(self, frame: np.ndarray):
        if frame is not None:
            frame = cv2.putText(img=frame, text=f"{self.agent.vehicle.transform}", org=(20, frame.shape[0] - 20),
                                fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.6,
                                color=(0, 255, 0), thickness=1, lineType=cv2.LINE_AA)
            frame = cv2.putText(img=frame,
                                text=f"{self.control_streamer.control_tx} | "
                                     f"max_throttle = {round(self.ios_config.max_throttle,3)} | "
                                     f"max_steering = {round(self.ios_config.max_steering,3)}",
                                org=(20, frame.shape[0] - 40), fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.6,
                                color=(0, 255, 0),  # BGR
                                thickness=1, lineType=cv2.LINE_AA)

        return frame
