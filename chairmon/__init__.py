"""椅子久坐检测（HCM · WT9011DCL-BT50 BLE 5.0 姿态传感器）。"""

from .model import (STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN, OccupancyConfig, Sample,
                    config_for)
from .occupancy import OccupancyDetector
from .sedentary import Reminder, SedentaryConfig, SedentaryTracker

__version__ = "0.1.0"
__all__ = ["STATE_EMPTY", "STATE_OCCUPIED", "STATE_UNKNOWN", "OccupancyConfig", "Sample",
           "config_for", "OccupancyDetector", "SedentaryTracker", "SedentaryConfig", "Reminder"]
