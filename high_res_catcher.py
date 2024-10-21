import Quartz
from AppKit import NSWorkspace
import pyautogui
from datetime import datetime
import time
from PIL import Image
from pynput import keyboard
import cv2
import sys
import numpy as np

def get_window_by_name(app_name):
    window_info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID
    )
    for window in window_info:
        if window['kCGWindowOwnerName'] == app_name:
            return window
    return None

def screenshot_app_window_high_res(app_name):
    target_window = get_window_by_name(app_name)
    if target_window:
        rect = target_window['kCGWindowBounds']
        x, y, width, height = int(rect['X']), int(rect['Y']), int(rect['Width']), int(rect['Height'])

        # Create a screenshot using Quartz for higher resolution
        display_id = Quartz.CGMainDisplayID()
        region = Quartz.CGRectMake(x, y, width, height)
        image_ref = Quartz.CGWindowListCreateImage(region, Quartz.kCGWindowListOptionIncludingWindow, target_window['kCGWindowNumber'], Quartz.kCGWindowImageDefault)
        
        # Convert to PIL image for saving
        width = Quartz.CGImageGetWidth(image_ref)
        height = Quartz.CGImageGetHeight(image_ref)
        bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
        data_provider = Quartz.CGImageGetDataProvider(image_ref)
        data = Quartz.CGDataProviderCopyData(data_provider)
        image_data = np.frombuffer(data, dtype=np.uint8).reshape((height, bytes_per_row // 4, 4))
        # Swap the Blue and Red channels (BGRA -> RGBA)
        image_data = image_data[..., [2, 1, 0, 3]]
        
        screenshot = Image.fromarray(image_data[:, :width, :4])  # Ignore the alpha channel
        
        # Save with timestamped filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'screenshot_high_res_{timestamp}.png'
        screenshot.save(filename)
        print(f"High-resolution screenshot of {app_name} taken and saved as '{filename}'")
        return filename
    else:
        print(f"No window found for application named {app_name}")

app_name = "JiangHu-mobile"

def on_press(key):
    try:
        if key.char == '`':
            screenshot_app_window_high_res(app_name)
    except AttributeError:
        pass

print("Press '`' to take a high-resolution screenshot of the app window.")

# Set up the listener
with keyboard.Listener(on_press=on_press) as listener:
    listener.join()




