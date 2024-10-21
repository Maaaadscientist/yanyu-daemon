import time
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

    print(f"Scaling Factors: scale_x = {scale_x}, scale_y = {scale_y}")

    # Desired position in the image
    click_dict_2 = {(pos['包裹']):1, (pos['紫阳琴']):1, (pos['弹奏']):1, (pos['阳关三叠']):1}
    time.sleep(3)
    def auto_click_event(click_list):
        time.sleep(1)
        for index, (key, value) in enumerate(click_list):
            print(index, (key, value))
            # Map to screen coordinates
            if type(key[0]) == int:
                image_click_x = key[0]
                image_click_y = key[1]
                time_gap = value
                window_click_x = window_left + image_click_x * scale_x
                window_click_y = image_click_y * scale_y +  window_top

                #window_click_x = image_click_x * scale_x
                #window_click_y = image_click_y * scale_y
                print(f"Screen Coordinates to Click: ({window_click_x}, {window_click_y})")

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
                
    #auto_click_event(click_dict_1)
    #auto_click_event(click_dict_0)
    #auto_click_event(click_dict_3)
    #auto_click_event(click_dict_4)
    #auto_click_event(click_dict_5)
    #auto_click_event(sleep1)
    auto_click_event(bear7)
    #auto_click_event(bear2)
    #auto_click_event(bear3)
    #auto_click_event(bear4)
    #auto_click_event(bear5)
    ##auto_click_event(sleep1)
    #auto_click_event(bear6)
    #temp_dict = {(pos['title']):1,(1203, 795):1}
    #auto_click_event(temp_dict)

if __name__ == "__main__":
    main()

