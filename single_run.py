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
        inverted_dict = {v:k for k, v in pos.items()} 
        time.sleep(0.5)
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
                print("click", f"{inverted_dict[key]}")

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
    if len(sys.argv) == 2:
        current_minute = int(sys.argv[1])
    elif len(sys.argv) == 3:
        current_minute = int(sys.argv[2])
        current_hour = int(sys.argv[1])
    else:
        current_minute = current_time.minute
        current_hour = current_time.hour
    print(f"minute set to: {current_minute}")
    print(f"hour set to: {current_hour}")
    min_1hour = current_minute + 1             
    min_2hour = current_minute + 12             
    min_3hour = current_minute + 15           
    min_5hour = current_minute + 20           
    min_6hour = current_minute + 18           
    second_lag = 0
    second_lag_short = 0
    # Schedule Events (hourly, every 5 minutes, and daily events)
    def schedule_events(current_hour, min_1hour, min_2hour, min_3hour, min_5hour, min_6hour, second_lag, second_lag_short):
        current_time = datetime.now()

        # Every 5-minute Events: Run every 5 minutes
        if current_time.minute % 6 == (current_minute % 6) and current_time.second == 20:
            print(f"Executing 5-minute interval events at {current_time}")
            auto_click_event(tanqin)
            auto_click_event(save)
            
        # Hourly Events: Run at the start of each hour, with 0.5 min error margin
        if current_time.minute == min_1hour  and current_time.second > second_lag:
            print(f"Executing 1-hourly events at {current_time}")
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
            auto_click_event(pig2)
            auto_click_event(pig1)
            second_lag += 4.5
        if current_time.minute == min_2hour and (current_time.hour - current_hour)% 2 ==0 and current_time.second > second_lag:
            print(f"Executing 2-hourly events at {current_time}")
            auto_click_event(sleep1)
            auto_click_event(xigua)
            auto_click_event(jiazhai)

            second_lag += 4.5
        if current_time.minute == min_3hour and (current_time.hour - current_hour)% 3 ==0 and current_time.second > second_lag:
            print(f"Executing 3-hourly events at {current_time}")
            auto_click_event(sleep1)
            auto_click_event(bear7)
            auto_click_event(bear14)

            second_lag += 4.5
        if current_time.minute == min_5hour and (current_time.hour - current_hour)% 5 ==0 and current_time.second > second_lag:
            print(f"Executing 5-hourly events at {current_time}")
            auto_click_event(xiangjiao)
            auto_click_event(shanzha)
            auto_click_event(pingguo)
            auto_click_event(lianou)

            second_lag += 4.5
        if current_time.minute == min_6hour and (current_time.hour - current_hour)% 6 ==0 and current_time.second > second_lag:
            print(f"Executing 5-hourly events at {current_time}")
            auto_click_event(jianshui)
            auto_click_event(hexia1)
            auto_click_event(hexia2)

            second_lag += 4.5

        # Daily Events: Run once per day at a specified time, e.g., 09:00 AM
        daily_event_time = current_time.replace(hour=5, minute=30, second=0, microsecond=0)
        if current_time >= daily_event_time and current_time < (daily_event_time + timedelta(seconds=30)):
            print(f"Executing daily events at {current_time}")

    
    auto_click_event(xiaotili)
    #auto_click_event(liushibieyuan)
    #auto_click_event(diling1)
    #auto_click_event(diling1)
    time.sleep(1)
    #while(True):
    #    auto_click_event(dazao)

if __name__ == "__main__":
    main()

