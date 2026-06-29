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
        

    def calculate_event_time(base_time, interval_minutes, compensate=0):
        """Calculate the event execution time based on the base time and an interval."""
        return base_time + timedelta(minutes=interval_minutes + compensate)
    
    # Get the current time and compensate if needed
    current_time = datetime.now()
    compensate = 0
    hour_lag = 0
    
    if len(sys.argv) == 2:
        compensate = int(sys.argv[1])
    elif len(sys.argv) == 3:
        hour_lag = int(sys.argv[2])
    current_minute = current_time.minute
    current_hour = current_time.hour
    
    # Set up initial event offsets
    min_1hour =   compensate
    min_2hour =   11 + compensate + 60 * hour_lag
    min_3hour =   14 + compensate + 60 * hour_lag
    min_5hour =   22 + compensate
    min_6hour =   20 + compensate
    min_day =   25 + compensate
    
    # Set up event intervals (in minutes)
    intervals = {
        "1_hour": 62,
        "2_hour": 122,
        "3_hour": 182,
        "5_hour": 302,
        "6_hour": 362,
        "daily": 1442  # 24 hours + compensation
    }
    
    # Calculate the initial execution times
    event_times = {
        "1_hour": calculate_event_time(current_time, min_1hour),
        "2_hour": calculate_event_time(current_time, min_2hour),
        "3_hour": calculate_event_time(current_time, min_3hour),
        "5_hour": calculate_event_time(current_time, min_5hour),
        "6_hour": calculate_event_time(current_time, min_6hour),
        "daily": calculate_event_time(current_time, min_day),
    }
    
    print(event_times)
    second_lag = 30
        
    def schedule_events(event_times, second_lag):
        current_time = datetime.now()
    
        # Check and execute each event based on its interval
        for name, event_time in event_times.items():
            if current_time >= event_time and current_time.second > second_lag:
                print(f"{name} event at {current_time}")
                if name == "1_hour":
                    auto_click_event(bear1)
                    auto_click_event(bear_tianshan)
                    auto_click_event(bear2)
                    auto_click_event(bear3)
                    auto_click_event(bear4)
                    auto_click_event(bear5)
                    auto_click_event(bear6)
                    auto_click_event(bear8)
                    auto_click_event(bear9)
                    auto_click_event(bear10)
                    auto_click_event(bear11)
                    auto_click_event(bear12)
                    auto_click_event(bear13)
                    auto_click_event(bear15)
                    auto_click_event(pig2)
                    auto_click_event(pig1)
                    auto_click_event(save)
                elif name == "2_hour":
                    auto_click_event(sleep1)
                    auto_click_event(xigua)
                    auto_click_event(jiazhai)
                    auto_click_event(save)
                elif name == "3_hour":
                    auto_click_event(sleep1)
                    auto_click_event(bear7)
                    auto_click_event(cow1)
                    auto_click_event(cow2)
                    auto_click_event(bear14)
                    auto_click_event(save)
                elif name == "5_hour":
                    auto_click_event(xiangjiao)
                    auto_click_event(shanzha)
                    auto_click_event(pingguo)
                    auto_click_event(changbaipingguo)
                    auto_click_event(lianou)
                    auto_click_event(save)
                elif name == "6_hour":
                    auto_click_event(jianshui)
                    auto_click_event(hexia1)
                    auto_click_event(hexia2)
                    auto_click_event(save)
                elif name == "daily":
                    auto_click_event(sleep2)
                    auto_click_event(suancai)
    
                # Reschedule event after its interval has passed
                event_times[name] = calculate_event_time(current_time, intervals[name] - second_lag)
                # Calculate the next execution time based on the original interval, ignoring execution time
                next_event_time = event_time + timedelta(minutes=intervals[name])
                event_times[name] = next_event_time
    
    # Main loop
    while True:
        schedule_events(event_times, second_lag)
        time.sleep(1)  # Check every second for precise execution

if __name__ == "__main__":
    main()

