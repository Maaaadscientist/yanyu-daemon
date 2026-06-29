import time
import sys, os
from datetime import datetime, timedelta
import pyautogui
from PIL import Image
from pynput.mouse import Listener

# macOS-specific imports
from AppKit import NSWorkspace, NSApplication
from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly
from coordinates import *
# Callback function for when the mouse button is pressed
def on_click(x, y, button, pressed):
    global mouse_pressed
    mouse_pressed = pressed
    if pressed:
        print("Mouse is down")
    else:
        print("Mouse is up")
    
def get_window_info(window_name):
    options = kCGWindowListOptionOnScreenOnly
    window_list = CGWindowListCopyWindowInfo(options, 0)

    for window in window_list:
        # Get the window's owner name and title
        owner_name = window.get('kCGWindowOwnerName', '')
        window_title = window.get('kCGWindowName', '')

        # Check if this is the window we're looking for
        if window_name in window_title or window_name in owner_name:
            # Get window bounds
            bounds = window.get('kCGWindowBounds', {})
            x = bounds.get('X', 0)
            y = bounds.get('Y', 0)
            width = bounds.get('Width', 0)
            height = bounds.get('Height', 0)

            # macOS uses a coordinate system with origin at bottom-left
            # Adjust y-coordinate
            from AppKit import NSScreen
            screen_height = NSScreen.screens()[0].frame().size.height
            y = screen_height - y - height

            return {
                'left': x,
                'top': y,
                'width': width,
                'height': height
            }
    return None

def main():
    # Replace with your game's window title or owner name
    game_title = 'JiangHu-mobile'
    game_title = '烟雨江湖'

    # Get the game window info
    window_info = get_window_info(game_title)

    screen_width, screen_height = pyautogui.size()
    print(f"Screen width: {screen_width}, Screen height: {screen_height}")
    if not window_info:
        print(f"No window found with title or owner '{game_title}'.")
        return

    window_left = window_info['left']
    window_width = window_info['width']
    window_height = window_info['height']
    window_top = screen_height - window_info['top'] - window_height

    print(f"Window Position: ({window_left}, {window_top})")
    print(f"Window Size: {window_width}x{window_height}")

    # Load your reference image
    image = Image.open('game_screenshot.png')
    image_width, image_height = image.size

    print(f"Image Size: {image_width}x{image_height}")

    # Calculate scaling factors
    scale_x = window_width / image_width
    scale_y = window_height / image_height


    # Desired position in the image
    click_dict_2 = {(pos['包裹']):1, (pos['紫阳琴']):1, (pos['弹奏']):1, (pos['阳关三叠']):1}
    time.sleep(3)
    def auto_click_event(click_list):
        time.sleep(1)
        for index, (key, value) in enumerate(click_list):
            # Map to screen coordinates
            if type(key[0]) == int:
                image_click_x = key[0]
                image_click_y = key[1]
                time_gap = value
                window_click_x = window_left + image_click_x * scale_x
                window_click_y = image_click_y * scale_y +  window_top

                #window_click_x = image_click_x * scale_x
                #window_click_y = image_click_y * scale_y

                # Optional: Wait before clicking
                time.sleep(time_gap)

                # Move and click
                pyautogui.moveTo(window_click_x, window_click_y, duration=0.1)
                pyautogui.click()
            else:
                drag_init_x = key[0][0]
                drag_init_y = key[0][1]
                drag_end_x = key[1][0]
                drag_end_y = key[1][1]
                window_drag_init_x = window_left + drag_init_x * scale_x
                window_drag_end_x = window_left + drag_end_x * scale_x
                window_drag_init_y = drag_init_y * scale_y +  window_top
                window_drag_end_y = drag_end_y * scale_y +  window_top
                pyautogui.moveTo(window_drag_init_x, window_drag_init_y)
                time.sleep(0.2)
                pyautogui.dragTo(window_drag_end_x, window_drag_end_y, button='left', duration=0.5)
                time.sleep(0.2)
                # Pause for a brief moment if needed
        time.sleep(2.5)
    current_time = datetime.now()
    compensate = 0
    if len(sys.argv) == 2:
        current_minute = int(sys.argv[1])
    elif len(sys.argv) == 3:
        current_minute = int(sys.argv[2])
        current_hour = int(sys.argv[1])
    elif len(sys.argv) == 4:
        current_minute = int(sys.argv[2])
        current_hour = int(sys.argv[1])
        compensate = int(sys.argv[3])
    else:
        current_minute = current_time.minute
        current_hour = current_time.hour
    print(f"minute set to: {current_minute}")
    print(f"hour set to: {current_hour}")
    print(f"compensate set to: {compensate}")
    min_1hour = current_minute + compensate 
    min_2hour = current_minute + 11 + compensate           
    min_3hour = current_minute + 14 + compensate
    min_5hour = current_minute + 19 + compensate
    min_6hour = current_minute + 17 + compensate
    min_day = current_minute + 24 + compensate
    second_lag = 30
    # Schedule Events (hourly, every 5 minutes, and daily events)
    def schedule_events(current_hour, min_1hour, min_2hour, min_3hour, min_5hour, min_6hour, min_day, second_lag):
        current_time = datetime.now()

        # Every 5-minute Events: Run every 5 minutes
        if current_time.minute % 2 == (current_minute % 6) and current_time.second == 20:
            print(f"Executing 5-minute interval events at {current_time}")
            auto_click_event(save)
            


    #auto_click_event(hexia2)
    #auto_click_event(pingguo)
    # Main loop
    while True:
        schedule_events(current_hour, min_1hour, min_2hour, min_3hour, min_5hour, min_6hour, min_day, second_lag)
        if current_time.minute == 0 and current_time.second == 0:
            min_1hour += 1
            min_2hour += 1
            min_3hour += 1
            min_5hour += 1
            min_6hour += 1
            second_lag += 3
            if second_lag >= 60:
               second_lag -= 60
               min_1hour += 1
               min_2hour += 1
               min_3hour += 1
               min_5hour += 1
               min_6hour += 1
        time.sleep(1)  # Check every second for precise execution

if __name__ == "__main__":
    main()

