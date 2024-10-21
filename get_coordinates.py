import cv2
import os
import re
import numpy as np
import time
from pynput import keyboard

# Function to display the RGB values and coordinates of the pixel under the cursor
def show_pixel_value(event, x, y, flags, param):
    if event == cv2.EVENT_MOUSEMOVE:
        pixel_value = img[y, x]
        text = f"X: {x}, Y: {y}, RGB: {pixel_value}"
        img_copy = img.copy()
        cv2.putText(img_copy, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow(window_name, img_copy)

# Function to find the latest screenshot in the directory
def find_latest_screenshot(directory):
    pattern = re.compile(r'screenshot_high_res_(\d{8})_(\d{6})\.png')
    screenshots = [f for f in os.listdir(directory) if pattern.match(f)]
    if not screenshots:
        return None
    
    latest_screenshot = max(screenshots, key=lambda f: pattern.match(f).groups())
    return os.path.join(directory, latest_screenshot)

# Set the directory where your screenshots are stored
screenshot_dir = '.'  # Current directory, change this if needed

# Create a named window for displaying images
window_name = "Image Analyzer"
cv2.namedWindow(window_name)

# Find the latest screenshot
latest_screenshot = find_latest_screenshot(screenshot_dir)
if latest_screenshot:
    print(f"Analyzing {latest_screenshot}")
    img = cv2.imread(latest_screenshot)
    
    if img is None:
        print(f"Error: {latest_screenshot} not found or cannot be opened.")

    # Set mouse callback function for displaying pixel values
    cv2.setMouseCallback(window_name, show_pixel_value)
    
    # Show the image and wait for a key press
    cv2.imshow(window_name, img)
    cv2.waitKey(0)
    
    # Close all OpenCV windows
    cv2.destroyAllWindows()
    
